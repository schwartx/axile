import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { PerformanceChart } from '@/components/viz/PerformanceChart'
import type { AnalysisChart, AnalysisExecutionSummary } from '@/lib/api/performance'

test('首屏图表仅凭轻量曲线和执行摘要显示读数与键盘操作', () => {
  const data: AnalysisChart['result'] = {
    range: 'all', engine_version: 'test', backtest_included: false,
    settings: { backtest_weight_type: 'cs', backtest_fee_rate: 0 }, baseline: null, end: null,
    observation_count: 2, record_count: 2, used_record_count: 2, invalid_asset_count: 0, skips: null,
    bindings: [], calendar: { status: 'not_required', calendar_id: null, label: null, closed_ranges: [], unavailable_ranges: [] },
    points: [1, 2].map(record_id => ({
      record_id, date: `2026-01-0${record_id}`, observed_at: `2026-01-0${record_id}T00:00:00Z`,
      account_return: record_id / 100, portfolio_return: null,
      account_daily_return: null, portfolio_daily_return: null, difference: null,
    })),
  }
  Object.defineProperty(data, 'executions', { get: () => { throw new Error('完整执行记录不属于图表输入') } })
  const execution: AnalysisExecutionSummary = {
    record_id: 2, execution_id: 'execution-2', time_ms: Date.parse('2026-01-02T00:00:00Z'),
    created_at: null, is_success: 1, noop: false, has_transactions: true,
    duration_sec: null, symbol_count: null, position_count: null, positions_present: false, reason: null,
    summary: { value: 123.4, cost: null, lossBp: null, coverage: null, count: 1, covered: 0, amountComplete: true, fees: {}, feeCovered: 0 },
  }
  const html = renderToStaticMarkup(createElement(PerformanceChart, {
    accountId: 7, data, executionIndex: [execution], daily: false, backtestMode: 'sized', scaleMode: 'natural',
    costs: new Map(), intervalCost: null, selection: null, onSelect: () => {}, portfolioNames: new Map(), showExecutions: true,
  }))
  expect(html).toContain('本次执行')
  expect(html).toContain('本次执行成交额 123.4')
  expect(html).toContain('数据不完整')
  expect(html).toContain('tabindex="0"')
  expect(html).toContain('data-chart="time-series"')
  expect(html).toContain('Enter / 空格选择')
  expect(html).not.toContain('持仓状态未知')
})
