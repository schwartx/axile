import { expect, test } from 'bun:test'
import { bindingAtObservation, bindingObservationSpan, chartAxis, executionMarkerPoint, executionMarkerTone, hasExecutionMarker, returnY, seriesKeys, type CanvasTheme, type ChartScene } from '@/components/viz/performanceCanvas'
import { pointTime } from '@/features/history/chartModel'
import { shanghaiTime } from '@/features/history/costs'
import type { AccountPerformance } from '@/types/api'
import type { AnalysisExecutionSummary, CostExecutionRow, TransactionPreview } from '@/lib/api/performance'

const summary = { value: 100, cost: 1, lossBp: 100, coverage: 1, covered: 1, count: 1, amountComplete: true, fees: {}, feeCovered: 0 }
const record = (success = 1) => ({ id: 1, execution_id: 'exec-1', created_at: '1970-01-01T00:00:00Z', is_success: success, raw_result: {} })
const trade = (time = 0): TransactionPreview => ({ symbol: 'A', side: 'buy', quantity: 1, price: 100, reference: 99, referenceSource: 'mid', time, endTime: time, timeEstimated: false, before: 0, target: 1, summary })
const row = (over: Partial<CostExecutionRow> = {}): CostExecutionRow => ({ key: '1', record: record(), summary, noop: false, symbolCount: 1, transactions: [trade()], positions: [], ...over })
const indexRow = (over: Partial<AnalysisExecutionSummary> = {}): AnalysisExecutionSummary => ({
  record_id: 1, execution_id: 'exec-1', time_ms: 0, created_at: null,
  is_success: 1, noop: false, has_transactions: true, duration_sec: null,
  symbol_count: null, position_count: null, positions_present: false, reason: null, summary, ...over,
})

test('轻索引只认实际成交标志，摘要覆盖率和执行质量保留原语义', () => {
  expect(hasExecutionMarker(indexRow({ has_transactions: false }))).toBe(false)
  expect(hasExecutionMarker(indexRow({ summary: { ...summary, count: 0 } }))).toBe(true)
  expect(executionMarkerTone(indexRow())).toBe('normal')
  expect(executionMarkerTone(indexRow({ is_success: null }))).toBe('warn')
  expect(executionMarkerTone(indexRow({ is_success: 0 }))).toBe('warn')
  expect(executionMarkerTone(indexRow({ summary: { ...summary, coverage: null } }))).toBe('warn')
  expect(executionMarkerTone(indexRow({ summary: { ...summary, count: 2 } }))).toBe('warn')
  expect(executionMarkerTone(indexRow({ noop: true, summary: { ...summary, coverage: null } }))).toBe('normal')
})

test('失败或成交覆盖不全走琥珀，其余一律中性', () => {
  expect(executionMarkerTone(row())).toBe('normal')
  expect(executionMarkerTone(row({ record: record(0) }))).toBe('warn')
  expect(executionMarkerTone(row({ summary: { ...summary, coverage: 0.5, covered: 1, count: 2 } }))).toBe('warn')
  expect(executionMarkerTone(row({ summary: { ...summary, coverage: null } }))).toBe('warn')
})

test('空跑与有利成交不再单独着色——质量细节由成本面板承载', () => {
  expect(executionMarkerTone(row({ noop: true, transactions: [], symbolCount: 0 }))).toBe('normal')
  expect(executionMarkerTone(row({ summary: { ...summary, lossBp: -20 } }))).toBe('normal')
})

test('无成交不画点（失败的也一样），有真实成交保留', () => {
  expect(hasExecutionMarker(row({ noop: true, transactions: [], symbolCount: 0 }))).toBe(false)
  expect(hasExecutionMarker(row({ noop: true, transactions: [], symbolCount: 0, record: record(0) }))).toBe(false)
  expect(hasExecutionMarker(row())).toBe(true)
})

const theme: CanvasTheme = { bg: '#fff', surface: '#fff', ink: '#000', muted: '#888', line: '#ddd', accent: '#06c', warn: '#fa0', fill: '#eee', font: 'sans-serif' }

test('绑定可见边界落在收益观测点，绑定详情仍保留实际生效时间', () => {
  const times = [100, 200, 300, 400]
  const data = { bindings: [
    { time: '1970-01-01T08:00:00.150+08:00', portfolio_id: 1 },
    { time: '1970-01-01T08:00:00.250+08:00', portfolio_id: 2 },
  ] } as AccountPerformance
  expect(bindingObservationSpan(data, times, 0)).toEqual({ start: 200, end: 300 })
  expect(bindingObservationSpan(data, times, 1)).toEqual({ start: 300, end: 400 })
  expect(bindingAtObservation(data, times, 275)?.binding.portfolio_id).toBe(1)
  expect(bindingAtObservation(data, times, 300)?.binding.portfolio_id).toBe(2)
  expect(bindingAtObservation(data, times, 300)?.binding.time).toBe(data.bindings[1].time)
})

test('回测口径统一选择累计和每日字段', () => {
  expect(seriesKeys(false, 'target')).toEqual(['account_return', 'target_portfolio_return'])
  expect(seriesKeys(true, 'target')).toEqual(['account_daily_return', 'target_portfolio_daily_return'])
  expect(seriesKeys(false, 'sized')).toEqual(['account_return', 'portfolio_return'])
})

function markerScene(returns: number[]): ChartScene {
  const data: AccountPerformance = {
    backtest_included: true,
    settings: { backtest_weight_type: 'cs', backtest_fee_rate: 0 },
    engine_version: 'test',
    range: 'all',
    baseline: null,
    end: null,
    record_count: returns.length,
    observation_count: returns.length,
    used_record_count: returns.length,
    invalid_asset_count: 0,
    skips: null,
    points: returns.map((value, index) => ({
      account_equity: 100,
      date: `2026-01-${String(index + 1).padStart(2, '0')}`,
      record_id: index + 1,
      execution_id: null,
      account_return: value,
      portfolio_return: value,
      account_daily_return: null,
      portfolio_daily_return: null,
      difference: 0,
    })),
    bindings: [],
    calendar: { status: 'not_required', calendar_id: null, label: null, closed_ranges: [], unavailable_ranges: [] },
  }
  const times = data.points.map(pointTime)
  return {
    data, times, width: 900,
    viewport: { start: shanghaiTime('2025-12-31T04:00:00Z'), end: shanghaiTime('2026-01-05T04:00:00Z') },
    daily: false, portfolioNames: new Map(), costs: null, theme,
  }
}

test('区间首尾外侧的执行吸附到最近观测点取值，x 仍按真实时间投影', () => {
  const scene = markerScene([0.05, 0.1, -0.02])
  const axis = chartAxis(scene)
  const first = scene.data.points[0], last = scene.data.points[scene.data.points.length - 1]
  const early = executionMarkerPoint(scene, row({ record: { ...record(), id: 901, created_at: '2025-12-31T04:00:00Z' } }))
  expect(early).not.toBeNull()
  expect(early!.y).toBeCloseTo(returnY(first.account_return!, axis), 10)
  const late = executionMarkerPoint(scene, row({ record: { ...record(), id: 902, created_at: '2026-01-05T04:00:00Z' } }))
  expect(late).not.toBeNull()
  expect(late!.y).toBeCloseTo(returnY(last.account_return!, axis), 10)
})

test('轻索引不依赖created_at或明细，标记保留真实执行时间和记录身份', () => {
  const scene = markerScene([0.05, 0.1, -0.02])
  const time = shanghaiTime('2026-01-02T04:00:00Z')
  const light = indexRow({ record_id: 2, time_ms: time })
  const point = executionMarkerPoint(scene, light)
  expect(point).not.toBeNull()
  expect(point!.y).toBeCloseTo(returnY(0.1, chartAxis(scene)), 10)
  const legacy = executionMarkerPoint(scene, row({ record: { ...record(), id: 2, created_at: new Date(time).toISOString() } }))
  expect(point).toEqual(legacy)
  expect(executionMarkerPoint(scene, indexRow({ time_ms: scene.viewport.end + 1 }))).toBeNull()
})
