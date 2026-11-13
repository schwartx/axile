import { useEffect, useState } from 'react'
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

export function DataTransferPage() {
  const [plan, setPlan] = useState<Plan>(empty)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [range, setRange] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const patch = (change: Partial<Plan>) => setPlan(p => ({ ...p, ...change }))
  useEffect(() => { apiGet<Settings>('/transfer/settings').then(s => { setSettings(s); setPlan({ ...empty, ...s.plan }); setRange(Boolean(s.plan.start || s.plan.end)) }).catch(e => setError(String(e))) }, [])
  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setMessage('')
    try { await action() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  const effective = { ...plan, ...(range ? {} : { start: null, end: null }) }
  return <section className="mx-auto max-w-[900px] px-5 py-8 md:px-8">
    <h1 className="text-[24px] font-semibold text-ink-1">数据传送</h1>
    <p className="mt-2 text-[14px] text-ink-3">主动从来源端拉取账户、组合、执行历史和录制。本机累积历史，来源端在收到确认后保留近期。</p>
    <fieldset disabled={busy || !settings} className="mt-8 space-y-7 disabled:opacity-70">
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
        <button className={buttonClass} onClick={() => void run(async () => { await apiSend('PUT', '/transfer/settings', effective); setMessage('传送规则已保存') })}>保存规则</button>
        <button className={buttonClass} disabled={!plan.source_url} onClick={() => void run(async () => { const result = await apiSend<{ batch: string }> ('POST', '/transfer/pull', effective); setMessage(`拉取完成 · ${result.batch}`) })}>立即拉取</button>
        <button className={buttonClass} onClick={() => void run(async () => {
          const response = await fetch('/api/v1/transfer/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(effective) })
          if (!response.ok) throw apiErrorFromBody(response.status, response.statusText, await response.json())
          download(await response.blob(), 'data.axile'); setMessage('数据包已导出；收到接收确认后才能清理')
        })}>导出数据包</button>
      </div>
    </fieldset>
    <section className="mt-9 border-t border-line pt-6">
      <h2 className="text-[17px] font-medium">接收数据</h2>
      <p className="mt-2 text-[13px] text-ink-3">首次接收需要空账户与组合，之后可重复接收同一来源。账户配置完整保留，导入后调度暂停。</p>
      <div className="mt-4 flex flex-wrap items-center gap-4">
        <label className="text-[14px]">上传数据包<input className={inputClass} type="file" accept=".axile" disabled={busy} onChange={e => { const f = e.target.files?.[0]; if (f) void run(async () => { const receipt = await apiUpload('/transfer/import', f); download(new Blob([JSON.stringify(receipt)], { type: 'application/json' }), 'receipt.json'); setMessage('接收完成，已下载确认文件；将确认文件交给发送端即可清理') }) }} /></label>
        <label className="text-[14px]">交回离线接收确认<input className={inputClass} type="file" accept=".json" disabled={busy} onChange={e => { const f = e.target.files?.[0]; if (f) void run(async () => { await apiSend('POST', '/transfer/acknowledge', { receipt: JSON.parse(await f.text()), plan: effective }); setMessage('接收确认已验证，按保存的保留规则清理') }) }} /></label>
      </div>
    </section>
    {busy && <p role="status" className="mt-5 text-[14px] text-ink-2">正在处理，请稍候…</p>}
    {message && <p role="status" className="mt-5 text-[14px] text-ink-2">{message}</p>}
    {error && <p role="alert" className="mt-5 whitespace-pre-wrap text-[14px] text-warn">{error}</p>}
    {settings?.last_run && <p className="mt-5 text-[13px] text-ink-3">最近定时拉取：{settings.last_run.ok ? '完成' : settings.last_run.error} · {settings.last_run.at}</p>}
  </section>
}
function localInput(value: string | null) {
  if (!value) return ''
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}
