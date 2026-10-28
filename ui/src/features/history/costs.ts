import { displayCurrencyUnit } from '@/lib/format'
import type { AccountActivityList } from '@/lib/api/accounts'
import type { ExecuteRecord } from '@/types/api'
import { dict, number, sideOf, shanghaiTime } from '@/features/account/executionValues'
import { loadJournal, type TimeWindow } from '@/features/account/journalActivity'
export { shanghaiTime } from '@/features/account/executionValues'

export const amount = (value: number | null) => value == null ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: Math.abs(value) < 0.01 ? 8 : 2 })
export const quantityText = (value: number | null) => value == null ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })
export const feeText = (summary: CostSummary) => Object.entries(summary.fees).map(([currency, value]) => `${amount(value)} ${displayCurrencyUnit(currency)}`).join(' · ') || '—'
export const coverageText = (summary: CostSummary) => summary.coverage == null
  ? `${summary.covered}/${summary.count} 笔有效` : `${(summary.coverage * 100).toFixed(1)}% 成交额覆盖`

/** 无快照游标的活动流，完整复读校验同数量原地更新；两遍一致才发布。 */
export async function loadPerformanceActivity(accountId: number, window: TimeWindow, signal: AbortSignal, fetchPage?: (skip: number) => Promise<AccountActivityList>) {
  const first = await loadJournal(accountId, window, signal, fetchPage, shanghaiTime)
  const second = await loadJournal(accountId, window, signal, fetchPage, shanghaiTime)
  if (JSON.stringify(first) !== JSON.stringify(second)) throw new Error('执行记录在加载期间发生变化，请刷新重试')
  return second
}

export function shanghaiDay(value: string): string {
  return new Date(shanghaiTime(value) + 8 * 3600000).toISOString().slice(0, 10)
}

export function shanghaiLabel(value: string): string {
  return new Date(shanghaiTime(value) + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ')
}

export interface CostTrade {
  record_id?: number
  execution_id?: string | null
  trade_id?: number
  symbol: string
  day: string
  time: number
  timeEstimated: boolean
  side: 'buy' | 'sell' | 'none'
  quantity: number | null
  price: number | null
  reference: number | null
  referenceSource: 'mid' | 'last' | null
  value: number | null
  cost: number | null
  lossBp: number | null
  fee: number | null
  feeCurrency: string | null
}

export interface CostSummary {
  value: number | null
  cost: number | null
  lossBp: number | null
  coverage: number | null
  covered: number
  count: number
  amountComplete: boolean
  fees: Record<string, number>
  feeCovered: number
  coveredValue?: number
  estimated?: number
}

const positive = (value: unknown) => { const n = number(value); return n != null && n > 0 ? n : null }

type CostRecord = Pick<ExecuteRecord, 'id' | 'execution_id' | 'created_at' | 'result'>

export function costTrades(record: CostRecord): CostTrade[] {
  return Object.entries(dict(dict(record.result).symbol_results)).flatMap(([symbol, raw]) => {
    const result = dict(raw)
    const tick = dict(result.first_tick)
    const bid = positive(tick.bid_price), ask = positive(tick.ask_price)
    const mid = bid != null && ask != null && ask >= bid ? (bid + ask) / 2 : null
    const reference = mid ?? positive(tick.last_price)
    const multiplier = positive(dict(result.sizing).unit_multiplier)
    const orders = new Map((Array.isArray(result.orders) ? result.orders : []).map(rawOrder => {
      const order = dict(rawOrder)
      return [String(order.order_id), sideOf(order.direction)]
    }))
    return (Array.isArray(result.trades) ? result.trades : []).map(rawTrade => {
      const trade = dict(rawTrade), extra = dict(trade.extra)
      const price = positive(trade.trade_price), quantity = positive(trade.trade_volume)
      const orderSide = orders.get(String(trade.order_id))
      const side = orderSide && orderSide !== 'none' ? orderSide : sideOf(extra.direction)
      const value = price != null && quantity != null && multiplier != null ? price * quantity * multiplier : null
      const cost = value != null && reference != null && side !== 'none'
        ? (side === 'buy' ? 1 : -1) * (price! - reference) * quantity! * multiplier! : null
      const actualTime = typeof trade.trade_time === 'string' && trade.trade_time.trim() ? shanghaiTime(trade.trade_time) : NaN
      const timeEstimated = !Number.isFinite(actualTime)
      const time = timeEstimated ? shanghaiTime(record.created_at) : actualTime
      return { record_id: record.id ?? undefined, execution_id: record.execution_id, symbol, day: new Date(time + 8 * 3600000).toISOString().slice(0, 10), time, timeEstimated,
        side, quantity, price, reference, referenceSource: mid != null ? 'mid' as const : reference != null ? 'last' as const : null,
        value, cost, lossBp: reference != null && price != null && side !== 'none' ? (price - reference) / reference * 1e4 * (side === 'buy' ? 1 : -1) : null,
        fee: number(extra.commission), feeCurrency: typeof extra.commission_asset === 'string' && extra.commission_asset.trim() ? extra.commission_asset : null }
    })
  })
}

export function summarizeCosts(trades: CostTrade[]): CostSummary {
  const valued = trades.filter(t => t.value != null)
  const valid = trades.filter(t => t.cost != null && t.value != null)
  const value = valued.reduce((sum, t) => sum + t.value!, 0)
  const coveredValue = valid.reduce((sum, t) => sum + t.value!, 0)
  const fees: Record<string, number> = {}
  let feeCovered = 0
  for (const trade of trades) if (trade.fee != null && trade.feeCurrency) {
    fees[trade.feeCurrency] = (fees[trade.feeCurrency] ?? 0) + trade.fee
    feeCovered++
  }
  return { value: valued.length ? value : null, cost: valid.length ? valid.reduce((sum, t) => sum + t.cost!, 0) : null,
    lossBp: coveredValue > 0 ? valid.reduce((sum, t) => sum + t.lossBp! * t.value!, 0) / coveredValue : null,
    coverage: valued.length === trades.length && value > 0 ? coveredValue / value : null,
    coveredValue, estimated: trades.filter(t => t.timeEstimated).length, covered: valid.length, count: trades.length, amountComplete: valued.length === trades.length, fees, feeCovered }
}

export interface CostExecution {
  key: string
  record: ExecuteRecord
  trades: CostTrade[]
  summary: CostSummary
  noop: boolean
}

export function executionsOnDay(executions: CostExecution[], day: string | null): CostExecution[] {
  if (!day) return executions
  return executions.flatMap(e => {
    const trades = e.trades.filter(t => t.day === day)
    return trades.length || shanghaiDay(e.record.created_at) === day ? [{ ...e, trades, summary: summarizeCosts(trades) }] : []
  })
}

export function costExecutions(records: ExecuteRecord[]): CostExecution[] {
  return records.map(record => {
    const trades = costTrades(record)
    const results = Object.values(dict(dict(record.result).symbol_results)).map(dict)
    const hasOrderFill = results.some(r => (Array.isArray(r.orders) ? r.orders : []).some(o => (number(dict(o).filled_volume) ?? 0) > 0))
    const noop = record.is_success === 1 && !trades.length && !hasOrderFill &&
      (dict(record.result).status === 'NOOP' || (results.length > 0 && results.every(r => r.status === 'NOOP')))
    return { key: String(record.id ?? record.execution_id), record, trades, summary: summarizeCosts(trades), noop }
  })
}

export function dailyCosts(executions: CostExecution[]): Map<string, CostSummary> {
  const groups = new Map<string, CostTrade[]>()
  for (const execution of executions) for (const trade of execution.trades) {
    const group = groups.get(trade.day) ?? []
    group.push(trade)
    groups.set(trade.day, group)
  }
  return new Map([...groups].map(([day, trades]) => [day, summarizeCosts(trades)]))
}

/** 完整执行耗时；缺失或非法值不显示为零。 */
export function durationText(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const rounded = Math.round(seconds)
  return `${Math.floor(rounded / 60)}m${rounded % 60}s`
}

/** 合并互不重叠的分组；有效成交额独立于完整度，不能从未知覆盖率反推。 */
export function combineCosts(groups: CostSummary[]): CostSummary {
  const sum = (key: 'count' | 'covered' | 'feeCovered') => groups.reduce((total, s) => total + s[key], 0)
  const nullableSum = (key: 'value' | 'cost') => groups.some(s => s[key] != null) ? groups.reduce((total, s) => total + (s[key] ?? 0), 0) : null
  const coveredValue = groups.reduce((total, s) => total + (s.coveredValue ?? (s.coverage != null && s.value != null ? s.coverage * s.value : 0)), 0)
  const value = nullableSum('value')
  const complete = groups.every(s => s.amountComplete)
  const fees: Record<string, number> = {}
  for (const s of groups) for (const [currency, fee] of Object.entries(s.fees)) fees[currency] = (fees[currency] ?? 0) + fee
  return { value, cost: nullableSum('cost'), count: sum('count'), covered: sum('covered'), coveredValue,
    lossBp: coveredValue > 0 ? groups.reduce((total, s) => total + (s.lossBp ?? 0) * (s.coveredValue ?? ((s.coverage ?? 0) * (s.value ?? 0))), 0) / coveredValue : null,
    coverage: complete && value != null && value > 0 ? coveredValue / value : null,
    amountComplete: complete, fees, feeCovered: sum('feeCovered'), estimated: groups.reduce((total, s) => total + (s.estimated ?? 0), 0) }
}

export const lossClass = (loss: number | null | undefined) => loss == null || loss === 0 ? 'text-ink-3' : loss > 0 ? 'text-warn' : 'text-accent'
