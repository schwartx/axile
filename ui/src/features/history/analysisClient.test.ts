import { expect, test } from 'bun:test'
import { decodeAnalysisChart, decodeAnalysisTable } from '@/features/history/analysisClient'
import { analysisTable, chartFixture } from '@/features/history/analysisTestFixtures'

test('table decoding follows column names and preserves null, zero and precision', () => {
  const rows = decodeAnalysisTable(analysisTable(['cost', 'value', 'quantity'], [[null, 0, 0.1234567890123456]]), ['value'])
  expect(rows).toEqual([{ cost: null, value: 0, quantity: 0.1234567890123456 }])
  expect(() => decodeAnalysisTable(analysisTable(['value', 'value'], [[1, 2]]))).toThrow('列名')
  expect(() => decodeAnalysisTable(analysisTable(['value'], [[]]))).toThrow('行长度')
  expect(() => decodeAnalysisTable(analysisTable(['value']), ['cost'])).toThrow('必要字段')
})

test('contribution decoding preserves zero and rejects invalid observations before display', () => {
  const response = chartFixture(812)
  const row = {idx:1, symbol:'fu2701',product:'SHFE.fu',currency:'CNY',gross_pnl:0,fee:0,fee_missing:0,issues:0,trades:1}
  response.contributions = [row]
  expect(decodeAnalysisChart(response).contributions?.[0]).toMatchObject({gross_pnl:0,fee:0})
  row.idx = 999
  expect(() => decodeAnalysisChart(response)).toThrow('贡献数据无效')
  row.idx = 1; row.fee_missing = -1
  expect(() => decodeAnalysisChart(response)).toThrow('贡献数据无效')
  row.fee_missing = 0; response.contributions.push({...row})
  expect(() => decodeAnalysisChart(response)).toThrow('贡献数据无效')
})

test('native chart keeps precision, null values and zero or rebated fees', () => {
  const response = chartFixture(810)
  response.daily_costs['2026-10-20'] = {value:0,cost:null,lossBp:null,coverage:null,covered:0,count:0,amountComplete:true,feeCovered:2,fees:{USD:0,EUR:-0.1234567890123456}}
  const chart = decodeAnalysisChart(response)
  expect(chart.result.points[1].portfolio_return).toBe(0.0123456789012345)
  expect(chart.daily_costs['2026-10-20'].cost).toBeNull()
  expect(chart.daily_costs['2026-10-20'].fees).toEqual({USD:0,EUR:-0.1234567890123456})
  response.result.points[1].portfolio_return = Infinity
  expect(() => decodeAnalysisChart(response)).toThrow('数值无效')
})

test('chart rejects a mismatched range or schema', () => {
  const response = chartFixture(811)
  response.range = '30'
  expect(() => decodeAnalysisChart(response)).toThrow('结构无效')
  response.result.range = '30'; Object.assign(response, {schema_version:2})
  expect(() => decodeAnalysisChart(response)).toThrow('结构无效')
})
