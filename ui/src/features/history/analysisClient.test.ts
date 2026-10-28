import { expect, test } from 'bun:test'
import { decodeAnalysisChart, decodeAnalysisTable } from '@/features/history/analysisClient'
import { analysisTable, chartFixture, indexColumns, summaryColumns } from '@/features/history/analysisTestFixtures'

test('table decoding follows column names and preserves null, zero and precision', () => {
  const rows = decodeAnalysisTable(analysisTable(['cost', 'value', 'quantity'], [[null, 0, 0.1234567890123456]]), ['value'])
  expect(rows).toEqual([{ cost: null, value: 0, quantity: 0.1234567890123456 }])
  expect(() => decodeAnalysisTable(analysisTable(['value', 'value'], [[1, 2]]))).toThrow('列名')
  expect(() => decodeAnalysisTable(analysisTable(['value'], [[]]))).toThrow('行长度')
  expect(() => decodeAnalysisTable(analysisTable(['value']), ['cost'])).toThrow('必要字段')
})

test('contribution decoding preserves zero and rejects invalid observations before display', () => {
  const response = chartFixture(812)
  response.tables.contributions = analysisTable(['idx', 'symbol', 'product', 'currency', 'gross_pnl', 'fee', 'fee_missing', 'issues', 'trades'], [[1, 'fu2701', 'SHFE.fu', 'CNY', 0, 0, 0, 0, 1]])
  expect(decodeAnalysisChart(response).contributions?.[0]).toMatchObject({ gross_pnl: 0, fee: 0, issues: 0 })
  response.tables.contributions.rows[0][0] = 999
  expect(() => decodeAnalysisChart(response)).toThrow('贡献数据无效')
  response.tables.contributions.rows[0][0] = 1
  response.tables.contributions.rows[0][6] = -1
  expect(() => decodeAnalysisChart(response)).toThrow('贡献数据无效')
  response.tables.contributions.rows[0][6] = 0
  response.tables.contributions.rows.push([...response.tables.contributions.rows[0]])
  expect(() => decodeAnalysisChart(response)).toThrow('贡献数据无效')
})

test('chart decoder preserves summary presence, actual zero fees and currency rebates', () => {
  const response = chartFixture(810)
  response.tables.daily_costs = analysisTable(['day', ...summaryColumns, 'present'], [['2026-10-20', 0, null, null, null, 0, 0, true, 2, 0, 0, 1023 & ~(1 << 1)]])
  response.tables.daily_fees.rows = [['2026-10-20', 'USD', 0], ['2026-10-20', 'EUR', -0.1234567890123456]]
  response.tables.execution_index = analysisTable(indexColumns, [[1, null, 1792425600000, null, null, true, false, null, null, null, false, null, null, null, null, null, 0, 0, true, 0, 0, 0]])
  response.tables.execution_fees.rows = [[1, 'CNY', 0, 1], [1, 'USD', -0.00000001, 1]]
  const chart = decodeAnalysisChart(response)
  expect(chart.result.points[1].portfolio_return).toBe(0.0123456789012345)
  expect(chart.daily_costs['2026-10-20'].value).toBe(0)
  expect(Object.hasOwn(chart.daily_costs['2026-10-20'], 'cost')).toBe(false)
  expect(chart.daily_costs['2026-10-20'].fees).toEqual({ USD: 0, EUR: -0.1234567890123456 })
  expect(chart.execution_index[0]).toMatchObject({ record_id: 1, created_at: null, positions_present: false, has_transactions: false, summary: { value: null, count: 0, amountComplete: true, fees: { CNY: 0, USD: -0.00000001 } } })
  expect(Object.hasOwn(chart.result, 'executions')).toBe(false)
  expect(Object.hasOwn(chart.execution_index[0], 'record')).toBe(false)
  expect(Object.hasOwn(chart.execution_index[0], 'positions')).toBe(false)
})

test('chart decoder restores only small calendar, binding and timeline structures', () => {
  const response = chartFixture(811)
  response.header.event_count = 105
  response.tables.events.rows = [['2026-10-20T00:00:00', '失败', '失败原因', 'execution-1']]
  response.tables.bindings.rows = [['2026-10-19T00:00:00', null]]
  response.tables.calendar_ranges.rows = [['closed', '2026-10-19', '2026-10-20'], ['unavailable', '2026-10-18', '2026-10-19']]
  const chart = decodeAnalysisChart(response)
  expect(chart.event_count).toBe(105)
  expect(chart.events).toEqual([{ time: '2026-10-20T00:00:00', tag: '失败', text: '失败原因', executionId: 'execution-1' }])
  expect(chart.result.bindings).toEqual([{ time: '2026-10-19T00:00:00', portfolio_id: null }])
  expect(chart.result.calendar.closed_ranges).toEqual([{ start: '2026-10-19', end: '2026-10-20' }])
  expect(chart.result.calendar.unavailable_ranges).toEqual([{ start: '2026-10-18', end: '2026-10-19' }])
  response.header.range = '30'
  expect(() => decodeAnalysisChart(response)).toThrow('版本或区间')
})
