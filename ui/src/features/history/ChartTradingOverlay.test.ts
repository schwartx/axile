import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { expect, test } from 'bun:test'
import { ChartTradingOverlay } from './ChartTradingOverlay'
import { executionDetailEntry, type ExecutionEvidence } from './analysisExecutionDetail'
import { ApiError } from '@/lib/api/client'
import type { AnalysisExecutionSummary } from '@/lib/api/performance'
import type { ChartScene } from '@/components/viz/performanceCanvas'

const summary = { value: null, cost: null, lossBp: null, coverage: null, covered: 0, count: 0, amountComplete: true, feeCovered: 0, fees: {} }
const rows: AnalysisExecutionSummary[] = [1, 2].map(record_id => ({ record_id, execution_id: `exec-${record_id}`, time_ms: 1000, created_at: null, is_success: 1, noop: false, has_transactions: true, duration_sec: null, symbol_count: null, position_count: null, positions_present: true, reason: `执行${record_id}原因`, summary }))
const scene = { data: { range: 'all' }, width: 600, viewport: { start: 0, end: 2000 } } as ChartScene
function overlay(snapshotId: string, pinned = true, recordId = 1) {
  return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(ChartTradingOverlay, {
    scene, rows, snapshotId, accountId: 81, currency: 'CNY', cursor: { time: 1000, x: 100, y: 100, recordId },
    selection: pinned ? { kind: 'execution', recordId, time: 1000 } : null, onSelect: () => {}, onInspect: () => {},
  })))
}
function evidence(overrides: Partial<ExecutionEvidence> = {}): ExecutionEvidence {
  return { recordId: 1, executionId: 'exec-1', createdAt: null, isSuccess: 1, rawResult: { status: 'SUCCEEDED' }, noop: false, reason: null, summary,
    positions: [], attempts: [], transactions: [], positionsComplete: true, symbolsComplete: true, transactionTotal: 0, nextOffset: null, ...overrides }
}

test('same-time hover respects record identity and shows summary without cached detail', () => {
  executionDetailEntry(81, 'hover', 1).state = { data: evidence({ positions: [{ idx: 0, symbol: 'SECRET_DETAIL', quantity: 1, direction: 'long' }] }), error: null, expired: false, loading: false }
  const html = overlay('hover', false, 1)
  expect(html).toContain('执行1原因')
  expect(html).not.toContain('执行2原因')
  expect(html).not.toContain('SECRET_DETAIL')
  expect(html).not.toContain('持仓明细')
  expect(html).toContain('role="tooltip"')
})

test('pinned loading remains explicit and never reuses another snapshot detail', () => {
  executionDetailEntry(81, 'loading', 1).state = { data: null, loading: true, error: null, expired: false }
  const html = overlay('loading')
  expect(html).toContain('正在读取本次执行详情…')
  expect(html).toContain('role="dialog"')
  expect(html).not.toContain('SECRET_DETAIL')
})

test('detail distinguishes unknown holdings from confirmed empty and incomplete holdings', () => {
  const entry = executionDetailEntry(81, 'positions', 1)
  entry.state = { data: evidence({ positions: null }), loading: false, error: null, expired: false }
  expect(overlay('positions')).toContain('持仓状态未知')
  entry.state = { ...entry.state, data: evidence() }
  expect(overlay('positions')).toContain('空仓')
  entry.state = { ...entry.state, data: evidence({ positionsComplete: false, nextOffset: 200 }) }
  const incomplete = overlay('positions')
  expect(incomplete).toContain('持仓明细尚未加载完整')
  expect(incomplete).not.toContain('空仓')
  expect(incomplete).toContain('加载更多明细')
  expect(incomplete).toContain('motion-reduce:transition-none')
  expect(incomplete).toContain('aria-expanded="false"')
})

test('expired detail offers a read action and no transaction or refresh action', () => {
  executionDetailEntry(81, 'gone', 1).state = { data: null, loading: false, error: new ApiError(410, 'gone'), expired: true }
  const html = overlay('gone')
  expect(html).toContain('该版本已过期')
  expect(html).toContain('重新读取绩效')
  expect(html).not.toContain('重试明细')
  expect(html).not.toContain('刷新绩效与成本')
})


test('a symbol already present on a partial holdings page is never shown as the complete post-execution position', () => {
  const entry = executionDetailEntry(81, 'partial-symbol', 1)
  entry.state = { loading: false, error: null, expired: false, data: evidence({
    positions: [{ idx: 0, symbol: 'rb', quantity: 2, direction: 'long' }], positionsComplete: false, nextOffset: 200, transactionTotal: 1,
    transactions: [{ idx: 0, symbol: 'rb', side: 'buy', quantity: 1, price: 100, reference: null, referenceSource: null, time: null, endTime: null, timeEstimated: false, before: 1, target: 2, summary }],
  }) }
  const html = overlay('partial-symbol')
  expect(html).toContain('执行后 持仓明细尚未加载完整')
  expect(html).not.toContain('执行后 多 2')
  expect(html).toContain('参考价来源未知')
})

test('pinning shows loading on the first render before its request effect runs', () => {
  expect(overlay('first-render')).toContain('正在读取本次执行详情…')
})
