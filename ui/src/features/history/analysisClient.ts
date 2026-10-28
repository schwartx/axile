import type { AnalysisChart, AnalysisChartResponse, AnalysisExecutionSummary, AnalysisTable, ContributionObservation } from '@/lib/api/performance'
import type { AccountPerformance, PerformancePoint } from '@/types/api'
import type { CostSummary } from '@/features/history/costs'

type Row = Record<string, unknown>

/** Decode only the requested small table; never synthesize missing scalar values. */
export function decodeAnalysisTable<T extends object = Row>(table: AnalysisTable, required: readonly string[] = []): T[] {
  if (!table || !Array.isArray(table.columns) || !Array.isArray(table.rows)) throw new Error('分析表结构无效')
  const columns = table.columns
  if (columns.some(column => typeof column !== 'string') || new Set(columns).size !== columns.length) throw new Error('分析表列名无效')
  if (required.some(column => !columns.includes(column))) throw new Error('分析表缺少必要字段')
  return table.rows.map(row => {
    if (!Array.isArray(row) || row.length !== columns.length) throw new Error('分析表行长度无效')
    return Object.fromEntries(columns.map((column, index) => [column, row[index]])) as T
  })
}

function contributionRows(table: AnalysisTable | undefined, pointCount: number) {
  if (!table) return undefined
  const rows = decodeAnalysisTable<ContributionObservation>(table, ['idx', 'symbol', 'product', 'currency', 'gross_pnl', 'fee', 'fee_missing', 'issues', 'trades'])
  const seen = new Set<string>()
  for (const row of rows) {
    const key = `${row.idx}:${row.symbol}`
    if (!Number.isInteger(row.idx) || row.idx < 0 || row.idx >= pointCount
      || typeof row.symbol !== 'string' || !row.symbol || typeof row.product !== 'string' || !row.product
      || row.currency != null && (typeof row.currency !== 'string' || !row.currency.trim())
      || row.gross_pnl != null && (typeof row.gross_pnl !== 'number' || !Number.isFinite(row.gross_pnl))
      || typeof row.fee !== 'number' || !Number.isFinite(row.fee)
      || [row.fee_missing, row.issues, row.trades].some(v => !Number.isSafeInteger(v) || v < 0)
      || seen.has(key)) throw new Error('品种贡献数据无效')
    seen.add(key)
  }
  return rows
}

function boolean(value: unknown): boolean {
  if (value === true || value === 1) return true
  if (value === false || value === 0) return false
  throw new Error('分析表布尔值无效')
}

const summaryColumns = ['value', 'cost', 'loss_bp', 'coverage', 'covered', 'count', 'amount_complete', 'fee_covered', 'covered_value', 'estimated'] as const
const summaryNames = ['value', 'cost', 'lossBp', 'coverage', 'covered', 'count', 'amountComplete', 'feeCovered', 'coveredValue', 'estimated'] as const

function costSummary(row: Row, fees: Record<string, number>, present?: number): CostSummary {
  const summary: Row = { fees }
  summaryColumns.forEach((column, index) => {
    if (present != null && !(present & (1 << index))) return
    summary[summaryNames[index]] = column === 'amount_complete' ? boolean(row[column]) : row[column]
  })
  return summary as unknown as CostSummary
}

function feeGroups(table: AnalysisTable, key: string): Map<unknown, Record<string, number>> {
  const grouped = new Map<unknown, Record<string, number>>()
  for (const row of decodeAnalysisTable<Row>(table, [key, 'currency', 'value'])) {
    const fees = grouped.get(row[key]) ?? {}
    Object.defineProperty(fees, row.currency as string, { value: row.value, enumerable: true, writable: true, configurable: true })
    grouped.set(row[key], fees)
  }
  return grouped
}

function executionIndex(response: AnalysisChartResponse): AnalysisExecutionSummary[] {
  const fees = feeGroups(response.tables.execution_fees, 'record_id')
  return decodeAnalysisTable<Row>(response.tables.execution_index, ['record_id', 'time', 'has_transactions', ...summaryColumns]).map(row => ({
    record_id: row.record_id as number,
    execution_id: row.execution_id as string | null,
    time_ms: row.time as number,
    created_at: row.created_at as string | null,
    is_success: row.is_success as number | null,
    noop: boolean(row.noop),
    has_transactions: boolean(row.has_transactions),
    duration_sec: row.duration_sec as number | null,
    symbol_count: row.symbol_count as number | null,
    position_count: row.position_count as number | null,
    positions_present: boolean(row.positions_present),
    reason: row.reason as string | null,
    summary: costSummary(row, fees.get(row.record_id) ?? {}),
  }))
}

function dailyCosts(response: AnalysisChartResponse): Record<string, CostSummary> {
  const fees = feeGroups(response.tables.daily_fees, 'day')
  return Object.fromEntries(decodeAnalysisTable<Row>(response.tables.daily_costs, ['day', 'present', ...summaryColumns]).map(row => [
    row.day as string, costSummary(row, fees.get(row.day) ?? {}, row.present as number),
  ]))
}

function performanceResult(response: AnalysisChartResponse): AnalysisChart['result'] {
  const { header, tables } = response
  const calendarRanges = decodeAnalysisTable<{ kind: string; start: string; end: string }>(tables.calendar_ranges, ['kind', 'start', 'end'])
  if (calendarRanges.some(row => row.kind !== 'closed' && row.kind !== 'unavailable')) throw new Error('分析日历区间类型无效')
  const ranges = (kind: string) => calendarRanges.filter(row => row.kind === kind).map(({ start, end }) => ({ start, end }))
  return {
    range: response.range,
    settings: response.settings,
    engine_version: header.engine_version,
    backtest_included: header.backtest_included,
    baseline: header.baseline,
    end: header.end,
    record_count: header.record_count,
    observation_count: header.observation_count,
    used_record_count: header.used_record_count,
    target_used_record_count: header.target_used_record_count,
    invalid_asset_count: header.invalid_asset_count,
    skips: header.skips,
    target_skips: header.target_skips,
    sizing_fallback_count: header.sizing_fallback_count,
    sizing_fallback_ranges: decodeAnalysisTable<NonNullable<AccountPerformance['sizing_fallback_ranges']>[number]>(tables.fallbacks, ['start', 'end', 'count']),
    points: decodeAnalysisTable<PerformancePoint>(tables.points, ['date', 'account_return', 'portfolio_return']),
    bindings: decodeAnalysisTable<AccountPerformance['bindings'][number]>(tables.bindings, ['time', 'portfolio_id']),
    calendar: { ...header.calendar, closed_ranges: ranges('closed'), unavailable_ranges: ranges('unavailable') },
  }
}

export function decodeAnalysisChart(response: AnalysisChartResponse): AnalysisChart {
  if (response.schema_version !== 1 || response.header.range !== response.range) throw new Error('绩效图表版本或区间无效')
  return {
    snapshot_id: response.snapshot_id,
    snapshot_version: response.snapshot_version,
    range: response.range,
    computed_at: response.computed_at,
    data_until: response.data_until,
    result: performanceResult(response),
    daily_costs: dailyCosts(response),
    events: decodeAnalysisTable<{ time: string; tag: string; text: string; execution_id: string | null }>(response.tables.events, ['time', 'tag', 'text', 'execution_id']).map(({ time, tag, text, execution_id }) => ({ time, tag, text, executionId: execution_id })),
    event_count: response.header.event_count,
    execution_index: executionIndex(response),
    contributions: contributionRows(response.tables.contributions, response.tables.points.rows.length),
  }
}
