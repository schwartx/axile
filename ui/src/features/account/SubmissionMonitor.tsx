import { useEffect, useState } from 'react'
import { apiGet, apiSend } from '@/lib/api/client'
import { InkRewrite } from '@/components/ui/InkRewrite'

export interface SubmissionMonitorData {
  settings: { order_limit: number | null; combined_limit: number | null; timezone: string; revision: number; updated_at_ms: number | null }
  control_date: string
  scope: string
  sampled_at_ms?: number
  metrics: { metric: 'order_calls' | 'combined_calls'; value: number; unknown: number; limit: number | null; state: 'disabled' | 'below' | 'unknown' | 'reached' }[]
  warnings: { event_uid: string; metric: string; value: number; limit: number; occurred_at_ms: number; control_date: string; timezone: string }[]
}

function parseSubmissionLimit(value: string): number | null {
  if (value.trim() === '') return null
  if (!/^[1-9][0-9]*$/.test(value) || Number(value) > 1_000_000_000) throw new Error('阈值须为 1～1000000000 的整数，留空关闭')
  return Number(value)
}

const names = { order_calls: '报单调用', combined_calls: '报撤合计' }

/** 原生调用阈值独立保存，展示持久化的当笔触发事实。 */
export function SubmissionMonitor({ accountId, live }: { accountId: number; live?: SubmissionMonitorData | null }) {
  const [data, setData] = useState<SubmissionMonitorData | null>(null)
  const [order, setOrder] = useState('')
  const [combined, setCombined] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    apiGet<SubmissionMonitorData>(`/account/${accountId}/orders/monitor`, controller.signal).then(value => {
      if (controller.signal.aborted) return
      setData(value); setOrder(String(value.settings.order_limit ?? '')); setCombined(String(value.settings.combined_limit ?? '')); setError(null)
    }).catch(reason => { if (!controller.signal.aborted) setError(String(reason instanceof Error ? reason.message : reason)) })
    return () => controller.abort()
  }, [accountId, attempt])
  const current = live && data && live.settings.revision >= data.settings.revision && (live.sampled_at_ms ?? 0) >= (data.sampled_at_ms ?? 0) ? live : data
  const dirty = data != null && (order !== String(data.settings.order_limit ?? '') || combined !== String(data.settings.combined_limit ?? ''))
  const save = async () => {
    try {
      const payload = { order_limit: parseSubmissionLimit(order), combined_limit: parseSubmissionLimit(combined) }
      setBusy(true); setError(null)
      const value = await apiSend<SubmissionMonitorData>('PUT', `/account/${accountId}/orders/monitor`, payload)
      setData(value); setOrder(String(value.settings.order_limit ?? '')); setCombined(String(value.settings.combined_limit ?? ''))
    } catch (reason) { setError(String(reason instanceof Error ? reason.message : reason)) }
    finally { setBusy(false) }
  }
  return <section aria-label="原生报撤监测" className="space-y-2 border-t border-line pt-3">
    <h3 className="text-sm font-[640]">原生报撤监测</h3>
    <p className="text-xs leading-5 text-ink-3">报撤合计＝报单调用＋撤单调用，包含同步拒绝；待确认调用单列。按{current?.settings.timezone ?? '账户时区'}自然日累计，仅覆盖已接入入口。监测阈值不改变交易额度。</p>
    {current?.metrics.map(metric => <p key={metric.metric} className={`num text-[13px] ${metric.state === 'reached' || metric.state === 'unknown' ? 'text-warn' : 'text-ink-2'}`}>
      {names[metric.metric]} {metric.value}{metric.limit == null ? ' · 预警关闭' : ` / ${metric.limit}`} · <InkRewrite text={metric.state === 'reached' ? '已达阈值' : metric.state === 'unknown' ? '存在待确认调用，是否达值未知' : metric.state === 'below' ? '已确认调用未达阈值' : '仅统计'} />{metric.unknown > 0 ? ` · 待确认 ${metric.unknown}` : ''}
    </p>)}
    {current?.warnings.map(event => <p key={event.event_uid} className="num text-xs text-warn">{event.metric === 'order_calls' ? '报单调用' : '报撤合计'}预警：触发时 {event.value}/{event.limit} · {new Date(event.occurred_at_ms).toLocaleTimeString('zh-CN', { timeZone: event.timezone })}</p>)}
    <div className="grid grid-cols-2 gap-3">
      <label className="text-xs text-ink-2">报单调用阈值<input aria-label="报单调用阈值" inputMode="numeric" placeholder="留空关闭" value={order} disabled={!data || busy} onChange={event => setOrder(event.target.value)} className="mt-1 h-8 w-full rounded-[7px] border border-line bg-surface px-2 text-sm outline-none focus:border-accent" /></label>
      <label className="text-xs text-ink-2">报撤合计阈值<input aria-label="报撤合计阈值" inputMode="numeric" placeholder="留空关闭" value={combined} disabled={!data || busy} onChange={event => setCombined(event.target.value)} className="mt-1 h-8 w-full rounded-[7px] border border-line bg-surface px-2 text-sm outline-none focus:border-accent" /></label>
    </div>
    <button type="button" disabled={!dirty || busy} onClick={() => void save()} className="text-[13px] text-accent disabled:text-ink-3">{busy ? '正在保存…' : '保存监测阈值'}</button>
    {error && <p role="alert" className="text-xs text-warn">{error} <button type="button" onClick={() => setAttempt(value => value + 1)} className="text-accent">重新读取</button></p>}
  </section>
}
