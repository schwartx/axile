import type { AnalysisChart, AnalysisChartResponse, AnalysisTable, ContributionObservation } from '@/lib/api/performance'

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

/** Native analysis already returns the display model; reject malformed numerical results. */
export function decodeAnalysisChart(response: AnalysisChartResponse): AnalysisChart {
  if (response.schema_version !== 1 || response.range !== response.result?.range || !Array.isArray(response.result.points)) throw new Error('绩效结果结构无效')
  for (const point of response.result.points) {
    if (typeof point.date !== 'string' || ['account_return', 'portfolio_return', 'account_equity'].some(key => {
      const value = (point as unknown as Row)[key]
      return value != null && (typeof value !== 'number' || !Number.isFinite(value))
    })) throw new Error('绩效曲线数值无效')
  }
  if (response.contributions) contributionRows({ columns: ['idx', 'symbol', 'product', 'currency', 'gross_pnl', 'fee', 'fee_missing', 'issues', 'trades'], rows: response.contributions.map(row => [row.idx, row.symbol, row.product, row.currency, row.gross_pnl, row.fee, row.fee_missing, row.issues, row.trades]) }, response.result.points.length)
  return response
}
