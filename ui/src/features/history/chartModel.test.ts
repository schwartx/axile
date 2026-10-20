import { expect, test } from 'bun:test'
import type { PerformancePoint, ExecuteRecord } from '@/types/api'
import { costExecutions, shanghaiTime, summarizeCosts } from '@/features/history/costs'
import { bindingSelection, clampViewport, intervalReturn, intervalSelection, navigationTimes, nearestIndex, pointTime, precisePoints, reconcileSelection, selectedExecutions, zoomViewport } from '@/features/history/chartModel'
import { xPosition, xTime } from '@/components/viz/performanceCanvas'
import type { AnalysisExecutionSummary } from '@/lib/api/performance'

const point = (date: string, value: number | null, observed_at = `${date}T17:00:00`): PerformancePoint => ({ date, observed_at, account_return: value, portfolio_return: value, account_daily_return: null, portfolio_daily_return: null, difference: null })
const indexedRow = (record_id: number, time_ms: number): AnalysisExecutionSummary => ({ record_id, execution_id: `exec-${record_id}`, time_ms, created_at: '', is_success: 1, noop: true, has_transactions: false, duration_sec: null, symbol_count: 0, position_count: null, positions_present: false, reason: null, summary: { value: null, cost: null, lossBp: null, coverage: null, count: 0, covered: 0, amountComplete: true, fees: {}, feeCovered: 0 } })

test('绑定时段仅选择内部观测，切换时刻属于下一段，末段包含末次观测', () => {
  expect(bindingSelection([10, 20, 30, 40], 15, 40)).toEqual({ kind: 'interval', start: 20, end: 30 })
  expect(bindingSelection([10, 20, 30, 40], 15, 40, true)).toEqual({ kind: 'interval', start: 20, end: 40 })
  expect(bindingSelection([10, 20, 30], 20, 30)).toBeNull()
  expect(bindingSelection([10, 20, 30], 31, 40)).toBeNull()
})

test('实际观测时间优先于日末标签，旧响应可绘图但不可精确比较', () => {
  const p = point('2026-01-01', 0)
  expect(pointTime(p)).toBe(shanghaiTime('2026-01-01T17:00:00'))
  const legacy = { ...p, observed_at: undefined }
  expect(pointTime(legacy)).toBe(shanghaiTime('2026-01-01T23:59:59'))
  expect(precisePoints([p, legacy])).toBe(false)
  expect(precisePoints([p, point('2026-01-02', 0.1)])).toBe(true)
})

test('区间按净值比复利，反向端点排序，同一时点不产生区间', () => {
  const points = [point('2026-01-01', 0.1), point('2026-01-02', 0.21)]
  const selection = intervalSelection(pointTime(points[1]), pointTime(points[0]))
  expect(intervalReturn(points, selection, 'account_return')).toBeCloseTo(0.1)
  expect(intervalSelection(1, 1)).toBeNull()
  expect(intervalReturn([{ ...points[0], account_return: -1 }, points[1]], selection, 'account_return')).toBeNull()
})

test('回测不跨缺口计算，账户仍能使用有效端点', () => {
  const points = [point('2026-01-01', 0.1), point('2026-01-02', null), point('2026-01-03', 0.21)]
  const selection = intervalSelection(pointTime(points[0]), pointTime(points[2]))
  expect(intervalReturn(points, selection, 'portfolio_return')).toBeNull()
  expect(intervalReturn(points.map((point, index) => ({ ...point, target_portfolio_return: index === 2 ? null : point.portfolio_return })), selection, 'target_portfolio_return')).toBeNull()
  expect(intervalReturn(points, selection, 'account_return')).toBeCloseTo(0.1)
  expect(intervalReturn(points, intervalSelection(pointTime(points[1]), pointTime(points[2])), 'account_return')).toBeNull()
})

test('刷新按真实时间保留选择，失效端点与旧接口清除选择', () => {
  const points = [point('2026-01-01', 0), point('2026-01-02', 0.1)]
  const selection = intervalSelection(pointTime(points[0]), pointTime(points[1]))
  expect(reconcileSelection(selection, [...points])).toBe(selection)
  expect(reconcileSelection(selection, points.slice(1))).toBeNull()
  expect(reconcileSelection(selection, points.map(p => ({ ...p, observed_at: undefined })))).toBeNull()
})

test('图表导航只需曲线和轻量执行时间，排序去重并排除非法时间', () => {
  const points = [point('1970-01-01', 0, '1970-01-01T00:00:03Z'), point('1970-01-01', 0, '1970-01-01T00:00:01Z'), point('invalid', null, 'invalid')]
  const executions = [indexedRow(4, 3000), indexedRow(2, 2000), indexedRow(1, 0), indexedRow(3, 2000), indexedRow(5, NaN), indexedRow(6, Infinity)]
  expect(navigationTimes(points, executions)).toEqual([0, 1000, 2000, 3000])
  expect(navigationTimes([], [])).toEqual([])
})

test('换快照后执行身份不存在就清除选择，同刻的其他执行不能替代', () => {
  const selection = { kind: 'execution' as const, recordId: 7, time: 1234 }
  expect(reconcileSelection(selection, [], [indexedRow(8, 1234)])).toBeNull()
  expect(reconcileSelection(selection, [], [])).toBeNull()
  expect(reconcileSelection(selection, [])).toBe(selection)
})

test('换快照保留同一执行身份并采用新索引真实时间，零时间有效', () => {
  const selection = { kind: 'execution' as const, recordId: 7, time: 1234 }
  expect(reconcileSelection(selection, [], [indexedRow(7, 2000)])).toEqual({ kind: 'execution', recordId: 7, time: 2000 })
  expect(reconcileSelection(selection, [], [indexedRow(7, 0)])).toEqual({ kind: 'execution', recordId: 7, time: 0 })
})

test('二分命中最近点，空数组与域外指针有明确边界', () => {
  expect(nearestIndex([], 0)).toBe(-1)
  expect(nearestIndex([0, 10, 30], -10)).toBe(0)
  expect(nearestIndex([0, 10, 30], 18)).toBe(1)
  expect(nearestIndex([0, 10, 30], 21)).toBe(2)
  expect(nearestIndex([0, 10, 30], 50)).toBe(2)
})

test('时间和像素映射互逆，缩放固定锚点且平移不越界', () => {
  const times = [0, 10, 20, 30, 40]
  const view = { start: 0, end: 40 }
  for (const width of [280, 1440]) expect(xTime(xPosition(13, width, view), width, view)).toBeCloseTo(13)
  expect(zoomViewport(view, 0.5, 20, times)).toEqual({ start: 10, end: 30 })
  expect(clampViewport({ start: -5, end: 15 }, times)).toEqual({ start: 0, end: 20 })
  expect(clampViewport({ start: 25, end: 45 }, times)).toEqual({ start: 20, end: 40 })
  expect(zoomViewport(view, 10, 20, times)).toEqual(view)
  const small = zoomViewport(view, 0.001, 20, times)
  expect(times.filter(t => t >= small.start && t <= small.end).length).toBeGreaterThanOrEqual(2)
})

test('区间逐笔采用左开右闭，保留跨日成交、替代时间与多币种收费', () => {
  const trade = (trade_time: string | undefined, currency: string, price = 101) => ({ trade_time, trade_price: price, trade_volume: 1, order_id: 'a', extra: { commission: 1, commission_asset: currency } })
  const record: ExecuteRecord = { account_id: 1, id: 1, execution_id: 'one', created_at: '2026-01-01T18:00:00', is_success: 1, input: {}, result: { symbol_results: { A: { sizing: { unit_multiplier: 1 }, first_tick: { last_price: 100 }, orders: [{ order_id: 'a', direction: 'buy' }], trades: [
    trade('2026-01-01T17:00:00', 'CNY'), trade(undefined, 'USD'), trade('2026-01-01T16:30:00Z', 'CNY'), trade('2026-01-02T17:00:00', 'CNY'), trade('2026-01-02T17:00:01', 'CNY'),
  ] } } } }
  const executions = costExecutions([record])
  const selection = intervalSelection(shanghaiTime('2026-01-01T17:00:00'), shanghaiTime('2026-01-02T17:00:00'))
  const filtered = selectedExecutions(executions, selection)
  expect(filtered[0].trades).toHaveLength(3)
  expect(filtered[0].trades.filter(t => t.timeEstimated)).toHaveLength(1)
  expect(filtered[0].summary.fees).toEqual({ USD: 1, CNY: 2 })
  expect(filtered[0].summary).toEqual(summarizeCosts(filtered.flatMap(e => e.trades)))
  expect(filtered[0].trades[1].day).toBe('2026-01-02')
  expect(selectedExecutions(executions, null)).toBe(executions)
})

test('无成交的执行仍按区间时间保留，缺失成本不补零', () => {
  const record: ExecuteRecord = { account_id: 1, id: 2, execution_id: null, created_at: '2026-01-02T10:00:00', is_success: 0, input: {}, result: {} }
  const executions = costExecutions([record])
  const filtered = selectedExecutions(executions, intervalSelection(shanghaiTime('2026-01-02T09:00:00'), shanghaiTime('2026-01-02T10:00:00')))
  expect(filtered).toHaveLength(1)
  expect(filtered[0].summary.cost).toBeNull()
  expect(filtered[0].summary.value).toBeNull()
})
