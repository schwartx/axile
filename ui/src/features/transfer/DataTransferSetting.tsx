import { useEffect, useId, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { Row, TEXT } from '@/features/account/editUi'
import { AccessibilitySwitch } from '@/components/ui/AccessibilitySwitch'
import { DatePicker } from '@/components/ui/DatePicker'
import { MacTimePicker } from '@/components/ui/MacTimePicker'
import { Segmented } from '@/components/ui/Segmented'
import { TransferSourceInput } from '@/features/transfer/TransferSourceInput'
import { DurationStepper } from '@/components/ui/DurationStepper'
import { positiveInteger, transferDraft, transferPlan, type TransferDraft, type TransferPlan } from '@/features/transfer/transferForm'
import { apiGet, apiSend, apiUpload, apiErrorFromBody } from '@/lib/api/client'

type Settings = { plan: Partial<TransferPlan>; last_run: { ok: boolean; at: string; error?: string } | null }
const retentionFields = [{ key: 'execution_days', label: '执行记录' }, { key: 'replay_days', label: '回放' }] as const
const buttonClass = 'cursor-pointer rounded-[9px] border border-line px-4 py-2 text-[14px] hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-45'
function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a'); link.href = url; link.download = name; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function DataTransferSetting() {
  const [open, setOpen] = useState(false)
  return <Row label="数据传送" span>
    <div className="flex items-center gap-4">
      <p className="min-w-0 flex-1 text-[13px] leading-5 text-ink-3">跨实例拉取与离线传送</p>
      <button type="button" aria-label="管理数据传送" aria-haspopup="dialog" aria-expanded={open}
        className="inline-flex h-9 shrink-0 cursor-pointer items-center gap-1 rounded-md px-2 text-[13px] text-ink-2 transition-colors duration-150 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent motion-reduce:transition-none"
        onClick={() => setOpen(true)}>管理<ChevronRight size={15} aria-hidden /></button>
    </div>
    <DataTransferModal open={open} onClose={() => setOpen(false)} />
  </Row>
}

function DataTransferModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [draft, setDraft] = useState(() => transferDraft())
  const [settings, setSettings] = useState<Settings | null>(null)
  const [range, setRange] = useState(false)
  const [busy, setBusy] = useState(false)
  const [offlineOpen, setOfflineOpen] = useState(false)
  const offlineId = useId()
  const automaticId = useId()
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const patch = (change: Partial<TransferDraft>) => { setDraft(p => ({ ...p, ...change })); setError('') }
  useEffect(() => {
    if (!open) return
    let alive = true
    setSettings(null); setError(''); setMessage(''); setOfflineOpen(false)
    apiGet<Settings>('/transfer/settings').then(s => {
      if (!alive) return
      const next = transferDraft(s.plan)
      setDraft(next); setRange(Boolean(s.plan.start || s.plan.end)); setSettings(s)
    }).catch(e => { if (alive) setError(String(e)) })
    return () => { alive = false }
  }, [open])
  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setMessage('')
    try { await action() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  const effective = () => transferPlan(draft, range)
  let formError = ''
  try { effective() } catch (cause) { formError = cause instanceof Error ? cause.message : String(cause) }
  const disabled = busy || !settings
  return <CenteredModal open={open} title="数据传送" onClose={onClose} closeDisabled={busy} widthClassName="w-[760px]"
    footer={<div className="mx-[22px] border-t border-line py-3">
      {busy && <p role="status" className="mb-3 text-[13px] text-ink-2">正在处理，请稍候…</p>}
      {message && <p role="status" className="mb-3 text-[13px] text-ink-2">{message}</p>}
      {(error || formError) && <p role="alert" className="mb-3 whitespace-pre-wrap text-[13px] text-warn">{error || formError}</p>}
      <fieldset disabled={disabled} className="flex justify-end gap-3">
        <button type="button" className={buttonClass} disabled={!!formError}
          onClick={() => void run(async () => { await apiSend('PUT', '/transfer/settings', effective()); setMessage('传送规则已保存') })}>保存规则</button>
        <button type="button" className="cursor-pointer rounded-[9px] border border-ink-1 bg-ink-1 px-4 py-2 text-[14px] font-medium text-surface hover:opacity-90 focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-45"
          disabled={!draft.source_url.trim() || !!formError} onClick={() => void run(async () => { const result = await apiSend<{ batch: string }>('POST', '/transfer/pull', effective()); setMessage(`拉取完成 · ${result.batch}`) })}>立即拉取</button>
      </fieldset>
    </div>}>
    <p className="mt-2 text-[14px] text-ink-3">从来源端拉取数据并在本机累积历史，来源端收到确认后才按规则清理。</p>
    <fieldset disabled={disabled} className="mt-5 space-y-5 disabled:opacity-70">
      <TransferSourceInput value={draft.source_url} disabled={disabled}
        onChange={source_url => patch({ source_url })} />
      <section>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="text-[14px] font-medium">时间范围</h3>
          <Segmented size="sm" value={range ? 'range' : 'all'} ariaLabel="时间范围"
            options={[{ value: 'all', label: '全部数据' }, { value: 'range', label: '指定范围' }]}
            onChange={value => { setRange(value === 'range'); setError('') }} />
        </div>
        <div inert={!range || disabled} aria-hidden={!range} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${range ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
          <div className="min-h-0 overflow-hidden">
            <p className="pt-3 text-xs text-ink-3">北京时间（UTC+8），留空的边界不限制范围</p>
            <div className="grid gap-4 pt-2 sm:grid-cols-2">
              {(['start', 'end'] as const).map(key => {
                const label = key === 'start' ? '开始' : '结束'
                return <div key={key} role="group" aria-label={`${label}时间（北京时间）`} className="min-w-0 space-y-2">
                  <p className="text-[13px] text-ink-2">{label}时间</p>
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <DatePicker value={draft[key].date} ariaLabel={`${label}日期`} className="flex-1"
                      min={key === 'end' ? draft.start.date || undefined : undefined}
                      max={key === 'start' ? draft.end.date || undefined : undefined}
                      onChange={date => patch({ [key]: { ...draft[key], date, time: date ? draft[key].time || '00:00' : '' } })} />
                    <MacTimePicker compact value={draft[key].time} ariaLabel={`${label}时刻`}
                      onChange={time => patch({ [key]: { ...draft[key], time } })} />
                  </div>
                </div>
              })}
            </div>
          </div>
        </div>
      </section>
      <section className="border-t border-line pt-4">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 id={automaticId} className="text-[14px] font-medium">自动拉取</h3>
            <p id={`${automaticId}-help`} className="mt-1 text-xs text-ink-3">关闭浏览器后仍由服务端定时执行</p>
          </div>
          <AccessibilitySwitch checked={draft.automatic} labelledBy={automaticId} describedBy={`${automaticId}-help`}
            onChange={automatic => patch({ automatic })} />
        </div>
        <div inert={!draft.automatic} aria-hidden={!draft.automatic} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${draft.automatic ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
          <div className="min-h-0 overflow-hidden">
            <div className="flex flex-wrap items-center gap-3 pt-3 text-[13px] text-ink-2">
              <span className="w-full sm:hidden">拉取间隔</span>
              <span className="hidden sm:inline">每隔</span>
              <DurationStepper value={draft.every_hours} min={1} max={8760} ariaLabel="拉取间隔"
                invalid={!positiveInteger(draft.every_hours, 8760)} onChange={every_hours => patch({ every_hours })} />
              <span className="hidden sm:inline">拉取一次</span>
            </div>
          </div>
        </div>
      </section>
      <section className="border-t border-line pt-4">
        <h3 className="text-[14px] font-medium">源端保留</h3>
        <p className="mt-1 text-xs text-ink-3">仅清理已确认接收的源端数据；留空则不清理</p>
        <div className="mt-3 grid gap-4 md:grid-cols-2">
          {retentionFields.map(({ key, label }) => <div key={key} role="group" aria-label={`${label}保留`} className="flex items-center gap-3">
            <span className="shrink-0 text-[13px] text-ink-2">{label}</span>
            <div className="max-w-full"><DurationStepper value={draft[key]} unit="days" min={1} emptyLabel="不清理" ariaLabel={`${label}保留天数`}
              invalid={!!draft[key].trim() && !positiveInteger(draft[key])} onChange={value => patch({ [key]: value })} /></div>
          </div>)}
        </div>
      </section>
    </fieldset>
    <section className="mt-6 border-t border-line pt-4">
      <button type="button" className="flex cursor-pointer items-center gap-2 py-1 text-[14px] text-ink-2 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent" aria-expanded={offlineOpen} aria-controls={offlineId} onClick={() => setOfflineOpen(value => !value)}>
        <ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${offlineOpen ? '' : '-rotate-90'}`} />离线传送
      </button>
      <div id={offlineId} inert={!offlineOpen} aria-hidden={!offlineOpen} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${offlineOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="min-h-0 overflow-hidden">
          <fieldset disabled={disabled} className="space-y-4 pt-3 disabled:opacity-70">
            <button type="button" className={buttonClass} disabled={!!formError} onClick={() => void run(async () => {
              const response = await fetch('/api/v1/transfer/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(effective()) })
              if (!response.ok) throw apiErrorFromBody(response.status, response.statusText, await response.json())
              download(await response.blob(), 'data.axile'); setMessage('数据包已导出；收到接收确认后才能清理')
            })}>导出数据包</button>
            <p className="text-[13px] text-ink-3">首次接收需要空账户与组合，之后可重复接收同一来源。账户配置完整保留，导入后调度暂停。</p>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="text-[14px]">上传数据包<input className={`${TEXT} mt-2 file:mr-3 file:border-0 file:bg-fill file:px-3 file:py-1 file:text-ink-2`} type="file" accept=".axile" onChange={e => { const f = e.target.files?.[0]; if (f) void run(async () => { const receipt = await apiUpload('/transfer/import', f); download(new Blob([JSON.stringify(receipt)], { type: 'application/json' }), 'receipt.json'); setMessage('接收完成，已下载确认文件；将确认文件交给来源端即可清理') }) }} /></label>
              <label className="text-[14px]">交回离线接收确认<input className={`${TEXT} mt-2 file:mr-3 file:border-0 file:bg-fill file:px-3 file:py-1 file:text-ink-2`} type="file" accept=".json" disabled={!!formError} onChange={e => { const f = e.target.files?.[0]; if (f) void run(async () => { await apiSend('POST', '/transfer/acknowledge', { receipt: JSON.parse(await f.text()), plan: effective() }); setMessage('接收确认已验证，按填写的保留规则清理') }) }} /></label>
            </div>
          </fieldset>
        </div>
      </div>
    </section>
    {settings?.last_run && <p className="mt-5 text-[13px] text-ink-3">最近定时拉取：{settings.last_run.ok ? '完成' : settings.last_run.error} · {settings.last_run.at}</p>}
  </CenteredModal>
}
