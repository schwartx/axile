import { apiGet } from '@/lib/api/client'
import type { AnalysisChart, AnalysisTable, CostQuery, PerformanceRange } from '@/lib/api/performance'
import { decodeAnalysisTable } from './analysisClient'
import type { CostSummary } from './costs'

export interface LocalInputs {
  protocol: 2
  kind: 'costs' | 'fees'
  algorithm: 'local-analysis-1'
  account_id: number
  version: string
  range: PerformanceRange
  initial_equity: number | null
  records: { record_id: string; observation: unknown; trades: unknown[] }[]
}
export type CostSelection = Pick<CostQuery, 'day' | 'start' | 'end' | 'record_id'>
interface InputPage extends Omit<LocalInputs, 'records'> {
  selection?: CostSelection
  records: { record_id: string; observation: unknown; trades: unknown[] | AnalysisTable }[]
  next_cursor: string | null
}
const tradeColumns = ['symbol', 'time_us', 'day', 'estimated', 'side', 'price', 'quantity', 'multiplier', 'reference', 'fee', 'fee_currency']
export async function loadAnalysisInputs(accountId: number, version: string, range: PerformanceRange, kind: LocalInputs['kind'], signal: AbortSignal, selection: CostSelection = {}, fetchPage = (cursor: string | null) => {
  const params = new URLSearchParams({ version, range, kind, ...Object.fromEntries(Object.entries(selection).filter(([, value]) => value != null).map(([key, value]) => [key, String(value)])), ...(cursor ? { cursor } : {}) })
  return apiGet<InputPage>(`/account/analysis/${accountId}/inputs?${params}`, signal)
}): Promise<LocalInputs> {
  let cursor: string | null = null, bytes = 0, trades = 0
  const seen = new Set<string>()
  let result: LocalInputs | null = null
  do {
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const page = await fetchPage(cursor)
    if (page.protocol !== 2 || page.kind !== kind || page.algorithm !== 'local-analysis-1' || page.account_id !== accountId || page.version !== version || page.range !== range
      || (['day', 'start', 'end', 'record_id'] as const).some(key => (page.selection?.[key] ?? null) !== (selection[key] ?? null))
      || result && page.initial_equity !== result.initial_equity) throw new Error('分析数据版本或选区已变更，请刷新绩效')
    bytes += new TextEncoder().encode(JSON.stringify(page)).byteLength
    const records = page.records.map(record => ({ ...record, trades: Array.isArray(record.trades) ? record.trades : decodeAnalysisTable(record.trades, tradeColumns) }))
    trades += records.reduce((n, record) => n + record.trades.length, 0)
    const count = (result?.records.length ?? 0) + page.records.length
    if (bytes > 16 * 1024 * 1024 || count > 20_000 || trades > 150_000) throw new Error('窗口超过本地分析上限，请缩短范围；正式绩效和后端明细仍可查看')
    if (!result) result = { ...page, records: [] }
    result.records.push(...records)
    cursor = page.next_cursor
    if (cursor && (seen.has(cursor) || !page.records.length)) throw new Error('分析分页未前进，请刷新重试')
    if (cursor) seen.add(cursor)
  } while (cursor)
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
  return result!
}

interface PreviewMode { start: number | null; used: number; skipped: number; closes: Record<string, { cumulative_return: number; daily_return: number | null }> }
export interface LocalResult {
  summary: CostSummary & { estimated: number }
  symbols: { symbol: string; summary: CostSummary }[]
  histogram: number[]
  unknown: number
}
export interface FeeResult {
  preview: { sized: PreviewMode; target: PreviewMode } | null
  preview_error: string | null
}
/** Keep the published account line and dates; replace only hypothetical portfolio returns. */
export function previewPerformance(chart: AnalysisChart, preview: NonNullable<FeeResult['preview']>, fee: number): AnalysisChart['result'] {
  const carried = { sized: null as number | null, target: null as number | null }
  return { ...chart.result, settings: { ...chart.result.settings, backtest_fee_rate: fee }, points: chart.result.points.map((point, index) => {
    const values = (mode: 'sized' | 'target') => {
      const input = preview[mode]
      if (index === 0 && point.account_daily_return == null) {
        const start = input.start == null ? null : input.start / 1000
        carried[mode] = start != null && start <= Date.parse(point.observed_at ?? point.date) ? 0 : null
        return { cumulative_return: carried[mode], daily_return: null }
      }
      const close = input.closes[point.date.slice(0, 10)]
      if (close) { carried[mode] = close.cumulative_return; return close }
      return { cumulative_return: carried[mode], daily_return: carried[mode] == null ? null : 0 }
    }
    const sized = values('sized'), target = values('target')
    return { ...point, portfolio_return: sized.cumulative_return, portfolio_daily_return: sized.daily_return,
      target_portfolio_return: target.cumulative_return, target_portfolio_daily_return: target.daily_return,
      difference: point.account_return != null && sized.cumulative_return != null ? sized.cumulative_return - point.account_return : null }
  }) }
}
