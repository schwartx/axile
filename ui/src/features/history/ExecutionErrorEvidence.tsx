import { useEffect, useRef, useState } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { ErrorCopyButton, ErrorDetailsContent } from '@/components/ui/ErrorDetailsContent'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { errorInfo, shortErrorReason } from '@/lib/errorInfo'
import { getExecuteRecords } from '@/lib/api/accounts'
import { getExecutionArtifacts, getExecutionEvents, getExecutionStatus } from '@/lib/api/executions'
import { buildExecutionErrorInfo, readEvidencePages, catalogEvidence, type EvidencePage } from '@/features/history/executionErrorAdapter'
import type { ExecuteRecord, ExecutionArtifact, ExecutionEvent, ExecutionStatus } from '@/types/api'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

interface Context { accountId: number; executionId?: string | null; recordId?: number | null; record?: ExecuteRecord | null; status?: ExecutionStatus | null }
type Source = 'record' | 'events' | 'artifacts' | 'status'
interface Snapshot {
  context: Context
  record: ExecuteRecord | null
  events: EvidencePage<ExecutionEvent> | null
  artifacts: EvidencePage<ExecutionArtifact> | null
  status: ExecutionStatus | null
  pending: Source[]
  errors: Partial<Record<Source, Error>>
}
const names: Record<Source, string> = { record: '执行记录', events: '执行事件', artifacts: '执行附件', status: '执行状态' }

function sourceScope(source: Source, snapshot: Snapshot): string {
  const page = source === 'events' ? snapshot.events : source === 'artifacts' ? snapshot.artifacts : null
  const range = page ? `${page.data.length}/${page.count ?? '未知总数'} 条` : ''
  if (snapshot.pending.includes(source)) return `读取中 ${range}`.trim()
  const error = snapshot.errors[source]
  if (error) return `读取失败；仅包含已读取的 ${range || '已有'}内容\n${errorInfo(error).text}`
  if (source === 'record') return snapshot.record ? '已读取选中记录' : '未生成或未保存记录'
  return range ? `${range}，读取完成` : '读取完成'
}

/** 打开时独立读取并冻结证据；页面轮询不会替换正文或复制内容。 */
export function ExecutionErrorEvidence(props: Context & { events?: ExecutionEvent[]; label?: string; enabled?: boolean; className?: string }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const requests = useRef(new Map<Source, AbortController>())
  const generation = useRef(0)
  const close = () => {
    generation.current += 1
    for (const controller of requests.current.values()) controller.abort()
    requests.current.clear()
    setSnapshot(null)
  }
  useEffect(() => {
    generation.current += 1
    const active = requests.current
    for (const controller of active.values()) controller.abort()
    active.clear()
    setSnapshot(null)
    return () => { generation.current += 1; for (const controller of active.values()) controller.abort(); active.clear() }
  }, [props.accountId, props.executionId, props.recordId, props.enabled])

  const load = async (source: Source, context: Context, version: number) => {
    requests.current.get(source)?.abort()
    const controller = new AbortController()
    requests.current.set(source, controller)
    setSnapshot(previous => previous && ({ ...previous, pending: [...new Set([...previous.pending, source])], errors: { ...previous.errors, [source]: undefined } }))
    let patch: Partial<Snapshot> = {}
    let failure: Error | undefined
    try {
      if (source === 'record') {
        if (context.record) patch.record = context.record
        else if (context.executionId || context.recordId != null) {
          const page = await getExecuteRecords(context.accountId, { execution_id: context.executionId ?? undefined, record_id: context.recordId ?? undefined, limit: 1 }, controller.signal)
          patch.record = page.data[0] ?? null
        }
      } else if (source === 'status') patch.status = await getExecutionStatus(context.executionId!, controller.signal)
      else if (source === 'events') {
        patch.events = await readEvidencePages((skip, limit) => getExecutionEvents(context.executionId!, controller.signal, { skip, limit }), controller.signal)
        failure = patch.events.error ?? undefined
      } else {
        patch.artifacts = await readEvidencePages((skip, limit) => getExecutionArtifacts(context.executionId!, controller.signal, { skip, limit }), controller.signal)
        failure = patch.artifacts.error ?? undefined
      }
    } catch (cause) { failure = cause instanceof Error ? cause : new Error(String(cause)) }
    if (controller.signal.aborted || version !== generation.current) return
    setSnapshot(previous => previous && ({ ...previous, ...patch, pending: previous.pending.filter(item => item !== source), errors: { ...previous.errors, [source]: failure } }))
  }
  const open = () => {
    close()
    const context: Context = { accountId: props.accountId, executionId: props.executionId, recordId: props.recordId ?? props.status?.record_id, record: props.record ? structuredClone(props.record) : null, status: props.status ? structuredClone(props.status) : null }
    const sources: Source[] = context.executionId ? ['record', 'status', 'events', 'artifacts'] : ['record']
    setSnapshot({ context, record: context.record ?? null, events: null, artifacts: null, status: context.status ?? null, errors: {}, pending: sources })
    for (const source of sources) void load(source, context, generation.current)
  }
  const failures = snapshot ? Object.entries(snapshot.errors).filter((entry): entry is [Source, Error] => !!entry[1]) : []
  // 已读取证据保持冻结；恢复时仅补齐此前失败的来源。
  useReadRecovery(() => {
    if (snapshot) return Promise.all(failures.map(([source]) => load(source, snapshot.context, generation.current)))
  }, snapshot != null)
  const busy = !!snapshot?.pending.length
  const info = snapshot ? buildExecutionErrorInfo({ ...snapshot.context, record: snapshot.record, status: snapshot.status,
    events: snapshot.events?.data, artifacts: snapshot.artifacts?.data,
    scope: (Object.keys(names) as Source[]).filter(source => snapshot.context.executionId || source === 'record')
      .map(source => ({ label: names[source], value: sourceScope(source, snapshot) })),
  }) : null
  return <div className={props.className ?? 'mt-3'}>
    {(props.events ?? []).map(event => { const item = catalogEvidence(event); return item && <div key={event.id ?? event.seq} className={`mb-1 text-[13px] ${event.event_type === 'instruments_failed' ? 'text-warn' : 'text-ink-2'}`}><span>{item.label.split(' · ')[0]}</span><span className="ml-2 whitespace-pre-wrap">{shortErrorReason(item.value)}</span></div> })}
    <button type="button" onClick={open} className="cursor-pointer text-[13px] text-accent hover:underline">{props.label ?? '查看错误详情'}</button>
    {snapshot && info && <CenteredModal open title="执行错误详情" subtitle={snapshot.context.executionId ?? `记录 ${snapshot.context.recordId}`} onClose={close} widthClassName="w-[720px]"
      footer={<div className="flex flex-wrap items-center justify-end gap-3 border-t border-line px-[22px] py-4">
        <ErrorCopyButton text={info.text} label="复制全部" disabled={busy || failures.length > 0} />
        {!busy && failures.length > 0 && <ErrorCopyButton text={info.text} label="复制已读取内容" />}
        <button type="button" onClick={close} className="cursor-pointer rounded-lg bg-ink-1 px-4 py-2 text-[13px] text-surface">关闭详情</button>
      </div>}>
      {busy && <p role="status" className="mb-3 text-[13px] text-ink-3">正在读取全部证据，完成后可复制全部。</p>}
      {failures.map(([source, error]) => <ErrorNotice intent="read" key={source} embedded title={`${names[source]}读取失败`} error={error} onRetry={() => load(source, snapshot.context, generation.current)} />)}
      <ErrorDetailsContent info={info} />
    </CenteredModal>}
  </div>
}
