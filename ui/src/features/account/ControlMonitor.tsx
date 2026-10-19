import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useEffect, useRef, useState } from 'react'
import { Segmented } from '@/components/ui/Segmented'
import { OverflowText } from '@/components/ui/OverflowText'
import { Tooltip } from '@/components/ui/Tooltip'
import { getAccountControlPolicy } from '@/lib/api/accounts'
import type { AccountControlPolicyEditorModel } from '@/types/api'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { Link } from '@/components/ui/nav'
import { ControlWaitingList } from '@/features/account/ControlActivity'
import {
  OPERATIONS, CONTROL_WINDOWS, operationName, metricLabel, controlCandidates, searchControlCandidates, canAddMetric,
  readControlPreferences,
  saveControlPreferences,
} from '@/features/account/controlPreferences'
import { useLiveControl, type ControlFrame, type ControlRule } from '@/stores/liveControl'
import { useDomainStore } from '@/stores/domain'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'
import { useControlQuotaWarnings } from '@/features/account/useControlQuotaWarnings'
import { quotaOperationName, quotaStageName } from '@/features/account/controlWarnings'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { SubmissionMonitor } from '@/features/account/SubmissionMonitor'
import { ControlUsageNotice } from '@/features/account/ControlUsage'

function metricRules(frame: ControlFrame | undefined): ControlRule[] {
  // 旧数据只提供每日摘要时保留兼容；新快照从规则取固定分钟用量。
  return [...(frame?.metrics ?? []), ...(frame?.rules ?? [])]
}
function controlValue(rule: ControlRule | undefined): string {
  if (!rule || rule.value == null) return '未知'
  if (rule.limit === 0) return `${rule.value}/0（禁用）`
  return rule.limit == null ? `${rule.value}（无限制）` : `${rule.value}/${rule.limit}`
}

function ControlDialog({ open, frame, accountId, accountName, selected, draft, onDraftChange, onSave, onClose, model, loading, failed, retry, names }: {
  model: AccountControlPolicyEditorModel | null
  loading: boolean
  failed: Error | null
  retry: () => void
  names: Record<string, string>
  open: boolean
  frame: ControlFrame | undefined
  accountId: number
  accountName: string | undefined
  selected: string[]
  draft: string[]
  onDraftChange: (keys: string[]) => void
  onSave: () => void
  onClose: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [query, setQuery] = useState('')
  const [operation, setOperation] = useState<string | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const addRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const rowsRef = useRef<HTMLDivElement>(null)
  const backRef = useRef<HTMLButtonElement>(null)
  const scroll = useRef(0)
  useEffect(() => { if (!open) { setAdding(false); setOperation(null); setQuery('') } }, [open])
  const candidates = searchControlCandidates(controlCandidates(model?.operations ?? [], Object.keys(model?.effective_policy.operations ?? {}), metricRules(frame)), query)
  const focusRow = (index: number) => requestAnimationFrame(() => rowsRef.current?.children[index]?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus())
  const add = (window: string) => {
    const id = `account:${operation}:${window}`
    if (!canAddMetric(draft, id)) return
    onDraftChange([...draft, id]); setAdding(false); setOperation(null); setQuery(''); focusRow(draft.length)
  }
  const dirty = JSON.stringify(draft) !== JSON.stringify(selected)
  const unknownWaiting = !frame || frame.status === 'unknown' || frame.status === 'incomplete'
  const warnings = useControlQuotaWarnings(frame)
  return <CenteredModal
    open={open}
    title="账户流控"
    subtitle={accountName ?? `账户 #${accountId}`}
    onClose={onClose}
    footer={<div className="mt-1 flex justify-end gap-2.5 border-t border-line px-5 py-3.5">
      <button type="button" className="cursor-pointer rounded-[9px] border border-line bg-surface px-4 py-2 text-sm text-ink-2" onClick={onClose}>取消</button>
      <button type="button" className="cursor-pointer rounded-[9px] border border-ink-1 bg-ink-1 px-[18px] py-2 text-sm font-[550] text-surface disabled:opacity-45" disabled={!dirty || draft.length === 0 || draft.length > 3} onClick={onSave}>保存</button>
    </div>}
  >
    <div className="space-y-5">
      <ControlUsageNotice frame={frame} />
      {frame?.order_submissions && <SubmissionMonitor key={accountId} accountId={accountId} live={frame.submission_monitor} />}
      {warnings.length > 0 && <section aria-label="额度预警" className="rounded-[10px] border border-warn/30 bg-warn/5 px-3 py-2">
        {warnings.map(warning => <div key={warning.id} className="py-1 text-[13px]">
          <p className="font-medium text-warn">今日{quotaOperationName(warning.operation)} · <InkRewrite text={quotaStageName(warning.stage)} /></p>
          <p className="num mt-1 text-xs text-ink-2">已用 {warning.used}/{warning.limit} · 剩余 {warning.remaining} 次{warning.percent != null ? ` · 预警线 ${warning.percent}%` : ''}{warning.stale ? ' · 数据待更新' : ''}</p>
        </div>)}
      </section>}
      <section aria-label="当前等待">
        <h3 className="mb-2 text-sm font-[640]">当前等待({unknownWaiting ? '未知' : frame.waiting_total})</h3>
        <div className="max-h-56 overflow-y-auto"><ControlWaitingList frame={frame} operationNames={names} /></div>
      </section>
      <section aria-label="摘要显示设置">
        <div className="flex justify-between"><h3 className="text-sm font-[640]">摘要显示设置</h3><span className="text-xs text-ink-3">{draft.length}/3</span></div>
        <ErrorNotice intent="read" hasData={model != null} title="操作目录加载失败" error={failed} embedded onRetry={retry} contextKey={open ? accountId : undefined} />
        <div ref={rowsRef} className="divide-y divide-line">
          {draft.map((id, index) => {
            const [, key, window] = id.split(':')
            const name = operationName(key, names)
            // 窗口是行内可变值，不参与行身份，避免切换时重建滑块。
            return <div key={`${key}:${index}`} className="flex items-center gap-2 py-2">
              <OverflowText text={name} className="min-w-0 flex-1 text-sm" />
              <div role="group" aria-label={`${name}时间窗口`}><Segmented size="sm" value={window} options={CONTROL_WINDOWS.map((item) => ({ ...item, disabled: item.value !== window && draft.includes(`account:${key}:${item.value}`), label: `${item.label}${item.value !== window && draft.includes(`account:${key}:${item.value}`) ? '（已添加）' : ''}` }))} onChange={(value) => {
                if (draft.includes(`account:${key}:${value}`)) return
                onDraftChange(draft.map((old, i) => i === index ? `account:${key}:${value}` : old)); focusRow(index)
              }} /></div>
              <button type="button" aria-label={`移除${name}${window === 'per_day' ? '今日' : '本分钟'}`} className="p-1 text-ink-3" onClick={() => {
                onDraftChange(draft.filter((old) => old !== id))
                if (draft.length > 1) focusRow(Math.min(index, draft.length - 2)); else requestAnimationFrame(() => addRef.current?.focus())
              }}>×</button>
            </div>
          })}
        </div>
        {draft.length < 3 ? <button ref={addRef} type="button" aria-expanded={adding} className="py-2 text-sm text-accent" onClick={() => {
          setAdding(!adding); setOperation(null)
          if (!adding) requestAnimationFrame(() => searchRef.current?.focus())
        }}>{adding ? '收起' : '＋ 添加指标'}</button> : <p className="py-2 text-xs text-ink-3">最多显示三项，移除一项后可添加</p>}
        <div inert={!adding || draft.length >= 3} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${adding && draft.length < 3 ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
          <div className="min-h-0 overflow-hidden">
            <div hidden={operation !== null}>
              <input ref={searchRef} aria-label="搜索操作" placeholder="搜索操作…" value={query} onChange={(event) => { setQuery(event.target.value); if (listRef.current) listRef.current.scrollTop = 0 }} className="mb-1 h-8 w-full rounded-[7px] border border-line bg-surface px-2.5 text-[13px] text-ink-1 outline-none placeholder:text-ink-3 focus:border-accent" />
              <div ref={listRef} className="max-h-[min(240px,35vh)] overflow-y-auto overscroll-contain">
                {candidates.map((row, index) => <div key={row.key}>
                  {!query.trim() && row.category !== candidates[index - 1]?.category && <h4 className="py-1 text-xs text-ink-3">{row.category}</h4>}
                  <button type="button" aria-label={`添加${row.name}`} className="flex w-full items-center gap-2 border-b border-line py-2 text-left text-sm" onClick={() => {
                    scroll.current = listRef.current?.scrollTop ?? 0; setOperation(row.key); requestAnimationFrame(() => backRef.current?.focus())
                  }}><OverflowText text={row.name} className="min-w-0 flex-1" /><span className="text-xs text-ink-3">{CONTROL_WINDOWS.filter((window) => draft.includes(`account:${row.key}:${window.value}`)).map((window) => `${window.label}已添加`).join(' · ')}</span><span aria-hidden>›</span></button>
                </div>)}
                {!candidates.length && <p className="py-2 text-xs text-ink-3">暂无匹配操作</p>}
              </div>
            </div>
            <div hidden={operation === null} className="space-y-2 pb-2">
              <button ref={backRef} type="button" className="text-sm text-accent" onClick={() => { setOperation(null); requestAnimationFrame(() => { if (listRef.current) listRef.current.scrollTop = scroll.current; searchRef.current?.focus() }) }}>返回操作列表</button>
              <p className="text-sm">{operationName(operation ?? '', names)}</p>
              <div className="flex gap-2">{CONTROL_WINDOWS.map((window) => {
                const exists = draft.includes(`account:${operation}:${window.value}`)
                return <button key={window.value} type="button" disabled={exists || draft.length >= 3} aria-label={`添加${operationName(operation ?? '', names)}${window.label}`} className="rounded-lg border border-line px-3 py-2 text-sm disabled:text-ink-3" onClick={() => add(window.value)}>{window.label}{exists ? '（已添加）' : ' ＋'}</button>
              })}</div>
            </div>
          </div>
        </div>
        {loading && <p className="text-xs text-ink-3" role="status">正在读取操作目录…</p>}
        <p className={`mt-2 text-xs ${draft.length === 0 ? 'text-warn' : 'text-ink-3'}`}>{draft.length === 0 ? '请至少选择一项摘要。' : '选择 1～3 项，保存后显示在账户摘要中。'}偏好仅保存在此浏览器此账户，不修改交易限额。</p>
      </section>

      <div className="border-t border-line pt-3">
        {(frame?.stale || frame?.incomplete) && <p className="mb-2 text-xs text-warn">{[frame.stale ? '数据陈旧，保留最后已知值' : '', frame.incomplete ? '统计可能不完整' : ''].filter(Boolean).join(' · ')}</p>}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Link to={`/accounts/${accountId}/edit/control`} onClick={onClose} className="text-[13.5px] font-semibold text-accent hover:underline">完整流控详情 →</Link>
          {frame?.sampled_at_ms && <span className="num text-xs text-ink-3">{new Date(frame.sampled_at_ms).toLocaleTimeString()} 更新</span>}
        </div>
      </div>
    </div>
  </CenteredModal>
}

export function AccountControlSummary({ accountId }: { accountId: number }) {
  return <AccountControlSummaryContent key={accountId} accountId={accountId} />
}

function AccountControlSummaryContent({ accountId }: { accountId: number }) {
  const frame = useLiveControl((state) => state.frames[accountId])
  const warnings = useControlQuotaWarnings(frame)
  const submissionWarnings = frame?.submission_monitor?.metrics.filter(metric => metric.state === 'reached') ?? []
  const accountName = useDomainStore((state) => state.accounts?.find((account) => account.account_id === accountId)?.name)
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState(() => readControlPreferences(accountId))
  const [draft, setDraft] = useState(selected)
  const [model, setModel] = useState<AccountControlPolicyEditorModel | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState<Error | null>(null)
  const [attempt, setAttempt] = useState(0)
  useReadRecovery(() => setAttempt(value => value + 1), open)
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setLoading(true); setFailed(null)
    getAccountControlPolicy(accountId, undefined, controller.signal).then((value) => {
      if (!controller.signal.aborted) setModel(value)
    }).catch((error) => { if (!controller.signal.aborted) setFailed(error instanceof Error ? error : new Error(String(error))) }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [open, accountId, attempt])
  const names = { ...OPERATIONS, ...Object.fromEntries((model?.operations ?? []).map((operation) => [operation.key, operation.display_name])) }

  const rules = metricRules(frame)
  const stale = Boolean(frame?.stale && !frame.waiting_total && !frame.incomplete)
  const updatedAt = frame?.sampled_at_ms != null
    ? new Date(frame.sampled_at_ms).toLocaleTimeString('zh-CN', { timeZone: frame.timezone ?? 'Asia/Shanghai', hour12: false })
    : null
  const freshness = updatedAt
    ? `${stale ? '统计待更新 · 最后更新于' : '统计更新于'} ${updatedAt}`
    : stale ? '统计待更新 · 尚无更新时间' : '尚未收到流控统计'
  const close = () => setOpen(false)
  const save = () => {
    if (draft.length < 1 || draft.length > 3) return
    setSelected([...draft])
    saveControlPreferences(accountId, draft)
    close()
  }
  return <>
    <Tooltip content={freshness}><button
      type="button"
      onClick={(event) => { event.stopPropagation(); setDraft([...selected]); setOpen(true) }}
      className="group flex min-w-0 max-w-full flex-none cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0 text-left text-[14px]"
    >
      <span className="flex flex-none items-center gap-1.5 text-ink-3">
        流控
        <span role={stale ? 'img' : undefined} aria-label={stale ? '统计待更新' : undefined} aria-hidden={!stale}
          className={`h-1.5 w-1.5 flex-none rounded-full ${stale ? 'bg-warn' : 'bg-transparent'}`} />
      </span>
      <span data-config-value className="num min-w-0 font-medium text-ink-1 group-hover:underline">
        {selected.map((id) => `${metricLabel(id, true, names)} ${controlValue(rules.find((rule) => rule.id === id))}`).join(' · ')}
      </span>
      {warnings.length > 0 && <span className="min-w-0 text-warn"><InkRewrite overflow text={warnings.map(warning => `${quotaOperationName(warning.operation)}${quotaStageName(warning.stage)}${warning.stale ? '（待更新）' : ''}`).join(' · ')} /></span>}
      {submissionWarnings.length > 0 && <span className="min-w-0 text-warn"><InkRewrite overflow text={submissionWarnings.map(metric => `${metric.metric === 'order_calls' ? '报单调用' : '报撤合计'}已达阈值${frame?.stale ? '（待更新）' : ''}`).join(' · ')} /></span>}
      {frame?.waiting_total ? <span className="flex-none text-warn">等待</span>
        : frame?.incomplete ? <span className="flex-none text-warn">不完整</span>
        : !stale && frame?.records.some((record) => record.decision === 'blocked') ? <span className="flex-none text-warn">部分请求阻断</span> : null}
    </button></Tooltip>
    <ControlDialog model={model} loading={loading} failed={failed} retry={() => setAttempt((value) => value + 1)} names={names} open={open} frame={frame} accountId={accountId} accountName={accountName} selected={selected} draft={draft} onDraftChange={setDraft} onSave={save} onClose={close} />
  </>
}
