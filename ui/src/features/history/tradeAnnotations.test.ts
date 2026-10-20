import { expect, test } from 'bun:test'
import { aggregateTransactions, executionAnnotations, executionAt, groupAnnotations, holdingAt } from '@/features/history/tradeAnnotations'
import type { AnalysisExecutionSummary, CostExecutionRow, TransactionPreview } from '@/lib/api/performance'
import type { ChartScene } from '@/components/viz/performanceCanvas'
import { chartAxis } from '@/components/viz/performanceCanvas'

const summary = { value: 100, cost: 1, lossBp: 100, coverage: 1, covered: 1, count: 1, amountComplete: true, fees: {}, feeCovered: 0 }
const trade = (time: number, side: TransactionPreview['side'] = 'buy'): TransactionPreview => ({ symbol: 'A', side, quantity: 1, price: 100, reference: 99, referenceSource: 'mid', time, endTime: time, timeEstimated: false, before: 0, target: 1, summary })
const row = (id: number, transactions: TransactionPreview[] = [], success = 1): CostExecutionRow => ({ key: String(id), record: { id, execution_id: `exec-${id}`, created_at: new Date(id * 1000).toISOString(), is_success: success, raw_result: {} }, summary, noop: !transactions.length, symbolCount: transactions.length, transactions, positions: [] })
const indexedRow = (record_id: number, time_ms: number): AnalysisExecutionSummary => ({ record_id, execution_id: `exec-${record_id}`, time_ms, created_at: '', is_success: 1, noop: true, has_transactions: false, duration_sec: null, symbol_count: 0, position_count: null, positions_present: false, reason: null, summary })
function scene(start = 0, end = 100000): ChartScene {
  return { width: 1000, viewport: { start, end }, times: [0, 100000], daily: false, costs: null, portfolioNames: new Map(), theme: { bg: '', surface: '', ink: '', muted: '', line: '', accent: '', warn: '', fill: '', font: '' }, data: { points: [{ date: '1970-01-01T00:00:00Z', observed_at: '1970-01-01T00:00:00Z', account_return: 0.1, portfolio_return: 0.1, account_daily_return: 0, portfolio_daily_return: 0, difference: 0 }, { date: '1970-01-01T00:01:40Z', observed_at: '1970-01-01T00:01:40Z', account_return: 0.2, portfolio_return: 0.2, account_daily_return: 0.1, portfolio_daily_return: 0.1, difference: 0 }], bindings: [] } as unknown as ChartScene['data'] }
}
test('只有真实成交与异常有标记，正常空跑不画点', () => {
  const items = executionAnnotations([row(1), row(2, [trade(2100), trade(2200, 'sell')]), row(3, [], 0)])
  expect(items.map(i => i.side)).toEqual(['buy', 'sell', 'warning'])
  expect(items.map(i => i.time)).toEqual([2100, 2200, 3000])
})
test('买卖分开聚合，缩小时保留所有成交，放大后展开', () => {
  const items = executionAnnotations(Array.from({ length: 5 }, (_, i) => row(i + 1, [trade(1000 + i * 500), trade(1000 + i * 500, 'sell')])))
  const wide = groupAnnotations(items, scene())
  const close = groupAnnotations(items, scene(500, 4000))
  expect(wide.length).toBe(2)
  expect(close.length).toBeGreaterThan(wide.length)
  expect(wide.flatMap(g => g.items).length).toBe(items.length)
  expect(close.flatMap(g => g.items).length).toBe(items.length)
})
test('指针位于执行之间时只展示之前的持仓，不能拿未来快照', () => {
  const rows = [row(1), row(2)]
  expect(holdingAt(rows, 999)).toBeNull()
  expect(holdingAt(rows, 1500)?.record.id).toBe(1)
  expect(holdingAt(rows, 2000)?.record.id).toBe(2)
})
test('轻量索引悬停只命中此前或同刻执行，不偷看未来执行', () => {
  const rows = [indexedRow(1, 1000), indexedRow(2, 2000)]
  expect(executionAt(rows, 999)).toBeNull()
  expect(executionAt(rows, 1500)).toBe(rows[0])
  expect(executionAt(rows, 2000)).toBe(rows[1])
  expect(executionAt(rows, 3000)).toBe(rows[1])
  expect(executionAt([], 1500)).toBeNull()
})
test('未知创建时间不妨碍零时间命中，同刻执行按记录顺序确定边界', () => {
  const rows = [indexedRow(1, 0), indexedRow(2, 0), indexedRow(3, 1000)]
  expect(executionAt(rows, -1)).toBeNull()
  expect(executionAt(rows, 0)).toBe(rows[1])
  expect(executionAt(rows, 999)).toBe(rows[1])
  expect(executionAt(rows, 1000)).toBe(rows[2])
})
test('放大到两个日末观测之间，坐标轴仍覆盖两端曲线', () => {
  const axis = chartAxis(scene(10000, 20000))
  expect(axis.min).toBeLessThanOrEqual(0.1)
  expect(axis.max).toBeGreaterThanOrEqual(0.2)
})
test('聚合展示全部成交的数量和加权均价，不取几条样例，也不混合买卖', () => {
  const a = trade(1000), b = { ...trade(2000), quantity: 3, price: 200, summary: { ...summary, value: 600, cost: 3, lossBp: 50 } }
  const result = aggregateTransactions(executionAnnotations([row(1, [a]), row(2, [b, trade(2100, 'sell')])]))
  expect(result).toHaveLength(2)
  expect(result[0].quantity).toBe(4)
  expect(result[0].price).toBe(175)
  expect(result[0].summary.cost).toBe(4)
  expect(result[0].summary.lossBp).toBeCloseTo((100 * 100 + 50 * 600) / 700)
  expect(result[1].side).toBe('sell')
})
