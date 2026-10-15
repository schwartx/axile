import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { Check, Download } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { DateRangePicker } from '@/components/ui/DateRangePicker'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Segmented } from '@/components/ui/Segmented'
import { getEvidencePackage } from '@/lib/api/evidence'
import { downloadBlob } from '@/lib/download'
import { useToastStore } from '@/stores/ui'
import {
  evidenceDateError, evidenceFilename,
  type EvidenceDates, type EvidenceOptions, type EvidenceProfile, type EvidenceTarget,
} from '@/features/account/evidenceExport'

interface Props {
  target: EvidenceTarget
  accountName?: string | null
  initialDates?: EvidenceDates
  fromSnapshot?: boolean
  isRunning?: boolean
  disabled?: boolean
}
const BUTTON = 'inline-flex min-h-9 cursor-pointer items-center justify-center gap-2 rounded-lg border border-line px-3 py-1.5 text-[13px] text-ink-2 hover:bg-fill disabled:cursor-default disabled:opacity-45'

/** 账户或执行身份变化时释放旧下载，避免迟到响应在新上下文触发保存。 */
export function EvidenceExportButton(props: Props) {
  const key = props.target.accountId + ':' + props.target.kind
    + (props.target.kind === 'execution' ? ':' + props.target.executionId : '')
  return <EvidenceExportControl key={key} {...props} />
}

function EvidenceExportControl({ target, accountName, initialDates, fromSnapshot, isRunning, disabled }: Props) {
  const [open, setOpen] = useState(false)
  const [dates, setDates] = useState<EvidenceDates>({ from: '', to: '' })
  const [profile, setProfile] = useState<EvidenceProfile>('shareable')
  const [includeLogs, setIncludeLogs] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const request = useRef<AbortController | null>(null)
  const toast = useToastStore(s => s.toast)
  const formId = useId()
  const dateError = target.kind === 'account' ? evidenceDateError(dates) : null
  const contextKey = [target.accountId, target.kind, dates.from, dates.to, profile, includeLogs].join(':')
  const title = target.kind === 'execution' ? '导出本次证据' : '导出账户证据'

  useEffect(() => () => {
    request.current?.abort()
    request.current = null
  }, [])

  const show = () => {
    setDates(initialDates ?? { from: '', to: '' })
    setProfile('shareable')
    setIncludeLogs(false)
    setError(null)
    setOpen(true)
  }
  const close = () => {
    request.current?.abort()
    request.current = null
    setBusy(false)
    setError(null)
    setOpen(false)
  }
  const submit = async (event?: FormEvent) => {
    event?.preventDefault()
    if (request.current || dateError) return
    const controller = new AbortController()
    request.current = controller
    const options: EvidenceOptions = { dates: { ...dates }, profile, includeLogs }
    setBusy(true)
    setError(null)
    try {
      const blob = await getEvidencePackage(target, options, controller.signal)
      if (controller.signal.aborted || request.current !== controller) return
      downloadBlob(blob, evidenceFilename(target, options))
      toast('证据包已生成，已发起下载。')
      close()
    } catch (cause) {
      if (!controller.signal.aborted && request.current === controller) setError(cause)
    } finally {
      if (request.current === controller) {
        request.current = null
        setBusy(false)
      }
    }
  }

  return <>
    <button type="button" className={BUTTON} disabled={disabled} onClick={show}>
      <Download size={15} aria-hidden />
      {target.kind === 'execution' ? '导出本次证据' : '按日期导出'}
    </button>
    <CenteredModal
      open={open}
      title={title}
      subtitle={accountName ?? '账户 #' + target.accountId}
      onClose={close}
      widthClassName={target.kind === 'account' ? 'w-[780px] md:max-h-[92vh]' : 'w-[480px]'}
      footer={<div className="flex items-center justify-end gap-3 border-t border-line px-[22px] py-4">
        <button type="button" className={BUTTON} onClick={close}>{busy ? '取消下载' : '取消'}</button>
        <button type="submit" form={formId} disabled={busy || !!dateError} aria-busy={busy}
          className="inline-flex min-h-9 w-44 cursor-pointer items-center justify-center rounded-lg border border-ink-1 bg-ink-1 px-3 py-2 text-sm font-medium text-surface disabled:cursor-default disabled:opacity-45">
          <InkRewrite text={busy ? '正在生成…' : '生成并下载 ZIP'} tone="label" />
        </button>
      </div>}
    >
      <form id={formId} onSubmit={submit} noValidate className="pb-1">
        <fieldset disabled={busy} className={target.kind === 'account'
          ? 'grid min-w-0 grid-cols-1 items-start gap-5 md:grid-cols-[minmax(0,1fr)_240px]'
          : 'min-w-0 space-y-5'}>
          {target.kind === 'account' ? <div>
            <DateRangePicker value={dates} describedBy={formId + '-dates'} disabled={busy}
              onChange={value => { setDates(value); setError(null) }} />
          </div> : <div className="space-y-2">
            <p className="break-all font-mono text-xs text-ink-2">{target.executionId}</p>
            {isRunning && <p className="text-xs leading-5 text-ink-3">本次执行仍在进行，证据包会注明采集时刻和未确认事项。</p>}
          </div>}

          <div className="min-w-0 space-y-4">
            <div>
              <div className="mb-2 text-sm text-ink-2">副本</div>
              <Segmented<EvidenceProfile> value={profile} options={[{ value: 'shareable', label: '分享副本' }, { value: 'internal', label: '内部诊断' }]}
                onChange={value => { setProfile(value); setError(null) }} />
              <p className="mt-2 text-xs leading-5 text-ink-3">
                <InkRewrite text={profile === 'shareable' ? '隐藏账户身份，保留交易数据。' : '保留名称和诊断信息，排除凭据与代码。'} tone="label"
                  className="motion-reduce:[&>.ink-rewrite-out]:hidden" />
              </p>
            </div>
            <button type="button" role="checkbox" aria-checked={includeLogs} aria-label="包含关联运行日志" disabled={busy}
              onClick={() => { setIncludeLogs(previous => !previous); setError(null) }}
              className="flex w-full cursor-pointer items-start gap-2.5 rounded-md text-left text-sm text-ink-2 outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-default disabled:opacity-45">
              <span aria-hidden className={'mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-[4px] border transition-colors duration-150 motion-reduce:transition-none '
                + (includeLogs ? 'border-accent bg-accent-soft text-accent' : 'border-border-strong bg-surface text-ink-3')}>
                <Check size={12} strokeWidth={2} className={'transition-opacity duration-150 motion-reduce:transition-none ' + (includeLogs ? 'opacity-100' : 'opacity-0')} />
              </span>
              <span>包含关联运行日志</span>
            </button>
            <div className="space-y-2 border-t border-line pt-3 text-xs leading-5 text-ink-3">
              {target.kind === 'account' && <>
                <p id={formId + '-dates'} className={dateError && dates.from && dates.to ? 'text-warn' : undefined}>
                  北京时间 · 含首尾 · {dateError ?? '最多 31 天'}
                </p>
                <p>导出完整执行，不受页面筛选影响。</p>
                {fromSnapshot && <p>按当前材料导出，可能与绩效快照不同。</p>}
              </>}
              <p role={busy ? 'status' : undefined}>
                {busy ? '正在生成证据包…' : 'ZIP：摘要、成交与审计材料。'}
              </p>
            </div>
          </div>
        </fieldset>
        <ErrorNotice title="证据导出失败" error={error} contextKey={contextKey} embedded
          onRetry={!busy && !dateError ? () => submit() : undefined} />
      </form>
    </CenteredModal>
  </>
}
