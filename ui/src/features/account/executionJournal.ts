import { executionOutcome, executionRecordSummary } from '@/features/account/executionOutcome'
import type { ExecuteRecord } from '@/types/api'
import type { AccountActivity } from '@/lib/api/accounts'
import { buildRecentActivity, recentRowText } from '@/features/account/recent'
import { dict } from '@/features/account/executionValues'
import { summarizeCosts, shanghaiTime, type CostTrade, type CostSummary } from '@/features/history/costs'
export { activityWindowQuery, loadJournal, type TimeWindow } from '@/features/account/journalActivity'
export { dict, number, sideOf } from '@/features/account/executionValues'

export type JournalRange = '7' | '30' | '90' | 'custom'
export interface JournalTrade extends CostTrade { key: string; executionId: string | null; slippageBps: number | null }
export interface Quality { value: number; nTrades: number; slippage: number | null; coverage: number; amountComplete: boolean }
export interface JournalExecution {
  record?: ExecuteRecord
  key: string; recordId: number | null; time: string; executionId: string | null
  status: string; warning: boolean; description: string; symbols: string[]
  trades: JournalTrade[]; summary: CostSummary | null; durationSec: number | null
}
export interface JournalSymbol extends Quality { symbol: string; lastTime: number; trades: JournalTrade[]; summary: CostSummary }
export const journalAmount = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
export const journalTime = (value: string) => value.replace('T', ' ').slice(0, 16)

/** 休市展示仍归入已有筛选分类，不落入执行失败。 */
export function matchesJournalStatus(row: Pick<JournalExecution, 'status'>, filter: string): boolean {
  if (filter === '全部状态' || row.status === filter) return true
  if (filter === '已跳过' && row.status === '休市跳过') return true
  return filter === '执行不到位' && row.status === '部分品种休市 · 执行未全部完成'
}

export const journalTrade = (trade: CostTrade, key: string): JournalTrade => ({ ...trade, key, executionId: trade.execution_id ?? null, slippageBps: trade.lossBp })
export function qualityOf(trades: JournalTrade[]): Quality {
  const s = summarizeCosts(trades)
  return { value: s.value ?? 0, nTrades: s.count, slippage: s.lossBp, coverage: s.coverage ?? 0, amountComplete: s.amountComplete }
}

/** 日历筛选固定上海时区，结束日包含全天。 */
export function journalWindow(range: JournalRange, from: string, to: string, now = new Date()) {
  const today = new Date(+now + 8 * 3600000).toISOString().slice(0, 10)
  const first = range === 'custom' ? from : today
  const last = range === 'custom' ? to : today
  const valid = (day: string) => /^\d{4}-\d{2}-\d{2}$/.test(day) && Number.isFinite(shanghaiTime(`${day}T00:00:00`)) && new Date(shanghaiTime(`${day}T00:00:00`) + 8 * 3600000).toISOString().slice(0, 10) === day
  if (!valid(first) || !valid(last)) return null
  const start = shanghaiTime(`${first}T00:00:00`) - (range === 'custom' ? 0 : (Number(range) - 1) * 86400000)
  const end = shanghaiTime(`${last}T00:00:00`) + 86400000
  return start < end ? { start, end } : null
}

/** 每次执行独占一行；复用现有状态归因，但不沿用近期摘要的连续折叠。 */
export function journalExecutions(activity: AccountActivity[]): JournalExecution[] {
  return activity.map((a) => {
    const recent = buildRecentActivity([a], { fetchLimit: 2 }).rows[0]
    const record = a.kind === 'execution' ? a.record : null
    const view = record ? executionOutcome(record) : null
    const status = view?.title ?? '已跳过'
    const symbols = record ? Object.keys(dict(record.symbol_results)) : []
    return {
      key: a.kind === 'execution' ? `execution:${record?.id ?? record?.execution_id}` : `skip:${a.id}`,
      time: a.occurred_at, executionId: record?.execution_id ?? null, status,
      warning: view?.warning ?? false,
      description: record ? executionRecordSummary(record, record.trade_count) : recentRowText(recent),
      symbols, trades: [], summary: record ? record.summary : summarizeCosts([]), recordId: record?.id ?? null, durationSec: record?.duration_sec ?? null,
    }
  })
}

export function journalSymbols(executions: JournalExecution[], keyword: string): JournalSymbol[] {
  const groups = new Map<string, JournalTrade[]>()
  for (const execution of executions) for (const trade of execution.trades) {
    if (!trade.symbol.toLowerCase().includes(keyword.trim().toLowerCase())) continue
    const group = groups.get(trade.symbol) ?? []
    group.push(trade)
    groups.set(trade.symbol, group)
  }
  return [...groups].map(([symbol, trades]) => {
    trades.sort((a, b) => b.time - a.time)
    return { symbol, trades, lastTime: trades[0].time, summary: summarizeCosts(trades), ...qualityOf(trades) }
  })
}
