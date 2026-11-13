import { useEffect, useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { Row } from '@/features/account/editUi'
import { apiGet, apiSend, apiUpload, apiErrorFromBody } from '@/lib/api/client'

type Plan = { start: string | null; end: string | null; source_url: string; every_hours: number; execution_days: number | null; replay_days: number | null }
type Settings = { plan: Partial<Plan>; last_run: { ok: boolean; at: string; error?: string } | null }
const empty: Plan = { start: null, end: null, source_url: '', every_hours: 0, execution_days: null, replay_days: null }
const inputClass = 'mt-2 block w-full rounded-[9px] border border-line bg-surface px-3 py-2 text-[14px] focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-45'
const buttonClass = 'cursor-pointer rounded-[9px] border border-line px-4 py-2 text-[14px] hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-45'
function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a'); link.href = url; link.download = name; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function DataTransferSetting() {
  const [open, setOpen] = useState(false)
  return <Row label="数据传送" hint="独立保存" span>
    <button type="button" className={buttonClass} onClick={() => setOpen(true)}>打开数据传送</button>
    <p className="mt-1.5 text-[12px] leading-5 text-ink-3">从其他实例拉取数据，或离线导入导出。规则独立保存，无需重启。</p>
    <DataTransferModal open={open} onClose={() => setOpen(false)} />
  </Row>
}

function DataTransferModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [plan, setPlan] = useState<Plan>(empty)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [range, setRange] = useState(false)
  const [busy, setBusy] = useState(false)
  const [offlineOpen, setOfflineOpen] = useState(false)
  const offlineId = useId()
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const patch = (change: Partial<Plan>) => setPlan(p => ({ ...p, ...change }))
  useEffect(() => {
    if (!open) return
    let alive = true
    setSettings(null); setError(''); setMessage(''); setOfflineOpen(false)
    apiGet<Settings>('/transfer/settings').then(s => {
      if (!alive) return
      setSettings(s); setPlan({ ...empty, ...s.plan }); setRange(Boolean(s.plan.start || s.plan.end))
    }).catch(e => { if (alive) setError(String(e)) })
    return () => { alive = false }
  }, [open])
  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setMessage('')
    try { await action() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  const effective = { ...plan, ...(range ? {} : { start: null, end: null }) }
  return <CenteredModal open={open} title="数据传送" onClose={onClose} closeDisabled={busy} widthClassName="w-[760px]">
    <p className="mt-2 text-[14px] text-ink-3">主动从来源端拉取账户、组合、执行历史和录制。本机累积历史，来源端在收到确认后保留近期。</p>
    <fieldset disabled={busy || !settings} className="mt-5 space-y-5 disabled:opacity-70">
      <div>
        <label className="text-[14px] text-ink-2">来源端地址<input className={inputClass} value={plan.source_url} placeholder="https://来源端地址:端口" onChange={e => patch({ source_url: e.target.value })} /></label>
      </div>
      <div>
        <label className="flex items-center gap-2 text-[14px]"><input type="checkbox" checked={range} onChange={e => setRange(e.target.checked)} />指定时间范围<span className="text-ink-3">默认传送至最新</span></label>
        <div className="mt-3 grid gap-4 md:grid-cols-2">
          <label className="text-[13px] text-ink-2">开始时间<input className={inputClass} type="datetime-local" disabled={!range} value={localInput(plan.start)} onChange={e => patch({ start: e.target.value ? new Date(e.target.value).toISOString() : null })} /></label>
          <label className="text-[13px] text-ink-2">结束时间<input className={inputClass} type="datetime-local" disabled={!range} value={localInput(plan.end)} onChange={e => patch({ end: e.target.value ? new Date(e.target.value).toISOString() : null })} /></label>
        </div>
      </div>
      <div className="grid gap-5 md:grid-cols-3">
        <label className="text-[14px] text-ink-2">定时拉取<input className={inputClass} type="number" min="0" max="8760" value={plan.every_hours} onChange={e => patch({ every_hours: Number(e.target.value) })} /><span className="mt-1 block text-[12px] text-ink-3">每隔多少小时，0 表示关闭</span></label>
        <label className="text-[14px] text-ink-2">源端执行记录保留天数<input className={inputClass} type="number" min="1" value={plan.execution_days ?? ''} onChange={e => patch({ execution_days: e.target.value ? Number(e.target.value) : null })} /><span className="mt-1 block text-[12px] text-ink-3">留空则不清理</span></label>
        <label className="text-[14px] text-ink-2">源端回放保留天数<input className={inputClass} type="number" min="1" value={plan.replay_days ?? ''} onChange={e => patch({ replay_days: e.target.value ? Number(e.target.value) : null })} /><span className="mt-1 block text-[12px] text-ink-3">仅清理已接收且不再使用的录制</span></label>
      </div>
      <div className="flex flex-wrap gap-3">
        <button type="button" className={buttonClass} onClick={() => void run(async () => { await apiSend('PUT', '/transfer/settings', effective); setMessage('传送规则已保存') })}>保存规则</button>
        <button type="button" className={buttonClass} disabled={!plan.source_url} onClick={() => void run(async () => { const result = await apiSend<{ batch: string }> ('POST', '/transfer/pull', effective); setMessage(`拉取完成 · ${result.batch}`) })}>立即拉取</button>
      </div>
    </fieldset>
    <section className="mt-6 border-t border-line pt-4">
      <button type="button" className="flex cursor-pointer items-center gap-2 py-1 text-[14px] text-ink-2 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent" aria-expanded={offlineOpen} aria-controls={offlineId} onClick={() => setOfflineOpen(value => !value)}>
        <ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${offlineOpen ? '' : '-rotate-90'}`} />离线传送
      </button>
      <div id={offlineId} inert={!offlineOpen} aria-hidden={!offlineOpen} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${offlineOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="min-h-0 overflow-hidden">
          <fieldset disabled={busy || !settings} className="space-y-4 pt-3 disabled:opacity-70">
            <button type="button" className={buttonClass} onClick={() => void run(async () => {
              const response = await fetch('/api/v1/transfer/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(effective) })
              if (!response.ok) throw apiErrorFromBody(response.status, response.statusText, await response.json())
              download(await response.blob(), 'data.axile'); setMessage('数据包已导出；收到接收确认后才能清理')
            })}>导出数据包</button>
            <p className="text-[13px] text-ink-3">首次接收需要空账户与组合，之后可重复接收同一来源。账户配置完整保留，导入后调度暂停。</p>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="text-[14px]">上传数据包<input className={inputClass} type="file" accept=".axile" onChange={e => { const f = e.target.files?.[0]; if (f) void run(async () => { const receipt = await apiUpload('/transfer/import', f); download(new Blob([JSON.stringify(receipt)], { type: 'application/json' }), 'receipt.json'); setMessage('接收完成，已下载确认文件；将确认文件交给来源端即可清理') }) }} /></label>
              <label className="text-[14px]">交回离线接收确认<input className={inputClass} type="file" accept=".json" onChange={e => { const f = e.target.files?.[0]; if (f) void run(async () => { await apiSend('POST', '/transfer/acknowledge', { receipt: JSON.parse(await f.text()), plan: effective }); setMessage('接收确认已验证，按填写的保留规则清理') }) }} /></label>
            </div>
          </fieldset>
        </div>
      </div>
    </section>
    {busy && <p role="status" className="mt-5 text-[14px] text-ink-2">正在处理，请稍候…</p>}
    {message && <p role="status" className="mt-5 text-[14px] text-ink-2">{message}</p>}
    {error && <p role="alert" className="mt-5 whitespace-pre-wrap text-[14px] text-warn">{error}</p>}
    {settings?.last_run && <p className="mt-5 text-[13px] text-ink-3">最近定时拉取：{settings.last_run.ok ? '完成' : settings.last_run.error} · {settings.last_run.at}</p>}
  </CenteredModal>
}
function localInput(value: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}
