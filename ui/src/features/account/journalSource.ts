import { getActivitySymbols } from '@/lib/api/accounts'
import { getPerformanceCosts, selectionQuery, type CostQuery, type CostExecutionRow, type CostSymbolRow, type CostPage, type PerformanceRange } from '@/lib/api/performance'
import { journalExecutions, type JournalExecution, type JournalSymbol } from '@/features/account/executionJournal'
import { activityWindowQuery, loadJournal, type TimeWindow } from '@/features/account/journalActivity'
export { activityWindowQuery }
import { combineCosts, shanghaiTime, shanghaiLabel, type CostSummary } from '@/features/history/costs'
import type { ChartSelection } from '@/features/history/chartModel'
import { executionRecordSummary, executionRecordView } from '@/features/account/executionOutcome'

export interface JournalData { executions: JournalExecution[]; symbols: JournalSymbol[]; dataUntil: string | null }
export type SnapshotScope = Omit<CostQuery, 'dimension' | 'cursor' | 'sort' | 'limit'>
export const SNAPSHOT_PARAMS = ['snapshot_id', 'performance_range', 'start', 'end', 'day', 'record_id']

export function parseJournalScope(params: URLSearchParams): { scope: SnapshotScope | null; error: Error | null } {
  if (!SNAPSHOT_PARAMS.some(key => params.has(key))) return { scope: null, error: null }
  const invalid = () => ({ scope: null, error: new Error('绩效范围链接无效，请返回实盘绩效重新选择') })
  const snapshot_id = params.get('snapshot_id')
  const range = params.get('performance_range')
  if (!snapshot_id || !['30', '90', 'all'].includes(range ?? '')) return invalid()
  const scope: SnapshotScope = { snapshot_id, range: range as PerformanceRange }
  if (params.has('start') || params.has('end')) {
    const start = Number(params.get('start')), end = Number(params.get('end'))
    if (!params.get('start') || !params.get('end') || !Number.isFinite(start) || !Number.isFinite(end) || start >= end || Math.abs(start) > 8.64e15 - 86400000 || Math.abs(end) > 8.64e15 - 86400000 || params.has('day')) return invalid()
    Object.assign(scope, { start, end })
  }
  if (params.has('day')) {
    const day = params.get('day')!
    const time = shanghaiTime(`${day}T00:00:00`)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || !Number.isFinite(time) || new Date(time + 8 * 3600000).toISOString().slice(0, 10) !== day) return invalid()
    scope.day = day
  }
  if (params.has('record_id')) {
    const id = Number(params.get('record_id'))
    if (!Number.isSafeInteger(id) || id <= 0 || scope.day || scope.start != null) return invalid()
    scope.record_id = id
  }
  return { scope, error: null }
}

export function performanceJournalPath(accountId: number, snapshotId: string, range: PerformanceRange, selection: ChartSelection): string {
  const params = new URLSearchParams({ snapshot_id: snapshotId, performance_range: range })
  for (const [key, value] of Object.entries(selectionQuery(selection))) params.set(key, String(value))
  return `/accounts/${accountId}/executions?${params}`
}
export function scopeLabel(scope: SnapshotScope): string {
  if (scope.start != null && scope.end != null) return `${shanghaiLabel(new Date(scope.start).toISOString())} → ${shanghaiLabel(new Date(scope.end).toISOString())}（不含起点，含终点）`
  if (scope.day) return `${scope.day}（上海时间）`
  if (scope.record_id) return '本次执行'
  return scope.range === 'all' ? '全部绩效范围' : `绩效近 ${scope.range} 天`
}

/** 只发布完整的轻量分组；游标断裂、重复或版本变化均拒绝。 */
export async function loadCostGroups<T>(accountId: number, query: CostQuery, signal: AbortSignal,
  fetchPage = (q: CostQuery) => getPerformanceCosts<T>(accountId, q, signal)): Promise<CostPage<T>> {
  const rows: T[] = [], cursors = new Set<string>(), identities = new Set<string>()
  let first: CostPage<T> | null = null, cursor: string | undefined
  do {
    signal.throwIfAborted()
    const page = await fetchPage({ ...query, limit: 100, cursor })
    signal.throwIfAborted()
    if (page.snapshot_id !== query.snapshot_id || (first && first.count !== page.count)) throw new Error('快照分页范围发生变化，请重新读取')
    first ??= page
    for (const row of page.data) {
      const grouped = row as { key?: string; symbol?: string }
      const identity = typeof row === 'object' && row != null ? grouped.key ?? grouped.symbol : String(row)
      if (identity != null && identities.has(identity)) throw new Error('快照分页发生重叠，请重试')
      if (identity != null) identities.add(identity)
      rows.push(row)
    }
    if (page.next_cursor && (!page.data.length || cursors.has(page.next_cursor))) throw new Error('快照分页不完整，请重试')
    if (page.next_cursor) cursors.add(page.next_cursor)
    cursor = page.next_cursor ?? undefined
  } while (cursor)
  if (!first || rows.length !== first.count) throw new Error('快照分页不完整，请重试')
  return { ...first, data: rows, next_cursor: null }
}

export function snapshotExecution(row: CostExecutionRow): JournalExecution {
  const view = executionRecordView(row.record)
  return { key: `execution:${row.record.id}`, recordId: row.record.id, executionId: row.record.execution_id,
    time: row.record.created_at, status: view.title, warning: view.warning,
    description: executionRecordSummary(row.record, row.tradeCount ?? (view.tradeCount || row.summary.count)),
    symbols: [...new Set([...(row.transactions ?? []).map(t => t.symbol), ...(row.attempts ?? []).map(a => a.symbol)])],
    trades: [], summary: row.summary, durationSec: row.durationSec ?? null }
}
export async function loadSnapshotJournal(accountId: number, scope: SnapshotScope, view: string, keyword: string, signal: AbortSignal): Promise<JournalData> {
  const query = { ...scope, symbol_search: keyword.trim() || undefined }
  if (view === 'symbols') {
    const page = await loadCostGroups<CostSymbolRow>(accountId, { ...query, dimension: 'symbol' }, signal)
    return { executions: [], dataUntil: page.data_until ?? null, symbols: page.data.map(row => ({ symbol: row.symbol, summary: row.summary,
      value: row.summary.value ?? 0, slippage: row.summary.lossBp, coverage: row.summary.coverage ?? 0,
      amountComplete: row.summary.amountComplete, nTrades: row.summary.count, trades: [], lastTime: row.lastTime ?? 0 })) }
  }
  const page = await loadCostGroups<CostExecutionRow>(accountId, { ...query, dimension: 'execution' }, signal)
  return { executions: page.data.map(snapshotExecution), symbols: [], dataUntil: page.data_until ?? null }
}
export async function loadLiveJournal(accountId: number, window: TimeWindow, signal: AbortSignal, view = 'executions'): Promise<JournalData> {
  if (view === 'symbols') {
    const page = await getActivitySymbols(accountId, activityWindowQuery(window), signal)
    return {
      executions: [],
      dataUntil: null,
      symbols: page.data.map((row) => ({
        symbol: row.symbol,
        summary: row.summary,
        value: row.summary.value ?? 0,
        slippage: row.summary.lossBp,
        coverage: row.summary.coverage ?? 0,
        amountComplete: row.summary.amountComplete,
        nTrades: row.n_trades,
        trades: [],
        lastTime: row.last_time,
      })),
    }
  }
  return { executions: journalExecutions(await loadJournal(accountId, window, signal)), symbols: [], dataUntil: null }
}
export function filterLiveExecutions(rows: JournalExecution[], keyword: string): JournalExecution[] {
  const word = keyword.trim().toLowerCase()
  if (!word) return rows
  return rows.filter(row => row.symbols.some(s => s.toLowerCase().includes(word)))
}
export function compareJournal(a: { summary: CostSummary | null; time?: string; lastTime?: number; key?: string; symbol?: string }, b: typeof a, sort: string): number {
  const field = sort === 'slippage' ? 'lossBp' : sort === 'cost' ? 'cost' : 'value'
  const av = a.summary?.[field], bv = b.summary?.[field]
  const metric = sort === 'time' ? 0 : av == null ? bv == null ? 0 : 1 : bv == null ? -1 : bv - av
  return metric || (b.time ? shanghaiTime(b.time) : b.lastTime ?? 0) - (a.time ? shanghaiTime(a.time) : a.lastTime ?? 0) || (a.key ?? a.symbol ?? '').localeCompare(b.key ?? b.symbol ?? '')
}
export const journalTotals = (rows: { summary: CostSummary | null }[]): CostSummary | null => rows.some(row => row.summary == null) ? null : combineCosts(rows.map(row => row.summary!))
