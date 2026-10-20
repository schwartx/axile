import { expect, test } from 'bun:test'
import { executionSelection, executionState } from '@/features/history/executionSelection'
import { selectionQuery } from '@/lib/api/performance'
import type { AnalysisExecutionSummary, CostExecutionRow } from '@/lib/api/performance'
import { snapshotPositions, quantityUnit } from '@/features/history/executionEvidenceModel'
import type { ExecutionArtifact } from '@/types/api'

const row = (patch: Partial<CostExecutionRow> = {}): CostExecutionRow => ({ key: '7', record: { id: 7, execution_id: 'exec-7', created_at: '2026-09-08T23:59:00', is_success: 1, raw_result: {} }, noop: true, symbolCount: 0, summary: { value: null, cost: null, lossBp: null, coverage: null, count: 0, covered: 0, amountComplete: true, fees: {}, feeCovered: 0 }, ...patch })
const indexedRow = (patch: Partial<AnalysisExecutionSummary> = {}): AnalysisExecutionSummary => ({ record_id: 7, execution_id: 'exec-7', time_ms: 1234, created_at: '', is_success: 1, noop: true, has_transactions: false, duration_sec: null, symbol_count: 0, position_count: null, positions_present: false, reason: null, summary: row().summary, ...patch })
test('单次执行按记录身份筛选，不带日期以保留跨日成交', () => {
  expect(selectionQuery(executionSelection(row()))).toEqual({ record_id: 7 })
  expect(executionSelection(row()).kind).toBe('execution')
})
test('轻量索引以真实毫秒时间选择执行，创建时间未知也不回填日期', () => {
  const selection = executionSelection(indexedRow())
  expect(selection).toEqual({ kind: 'execution', recordId: 7, time: 1234 })
  expect(selectionQuery(selection)).toEqual({ record_id: 7 })
})
test('真实零时间保留，同刻执行仍按记录身份分别选择', () => {
  const first = executionSelection(indexedRow({ record_id: 7, time_ms: 0 }))
  const second = executionSelection(indexedRow({ record_id: 8, time_ms: 0 }))
  expect(first).toEqual({ kind: 'execution', recordId: 7, time: 0 })
  expect(second).toEqual({ kind: 'execution', recordId: 8, time: 0 })
  expect(selectionQuery(first)).toEqual({ record_id: 7 })
  expect(selectionQuery(second)).toEqual({ record_id: 8 })
})
test('新结论与旧记录中性标题，不根据持仓数量推断执行成败', () => {
  expect(executionState(row({ positionCount: 0 }))).toBe('执行状态未知')
  expect(executionState(row({ record: { ...row().record, raw_result: { status: 'PARTIAL', symbol_results: { m2701: { status: 'PARTIAL' } } } } }))).toBe('执行未全部完成')
  expect(executionState(row({ record: { ...row().record, raw_result: { status: 'FAILED', error: '拒单' } } }))).toBe('执行失败')
})
test('缺失和退化快照不当成空仓，完整空数组才是空仓', () => {
  const artifacts = (content: unknown) => [{ artifact_type: 'account_snapshot', content }] as ExecutionArtifact[]
  expect(snapshotPositions([], 'account_snapshot')).toBeNull()
  expect(snapshotPositions(artifacts({ source: 'assumed', account_assets: { positions: [] } }), 'account_snapshot')).toBeNull()
  expect(snapshotPositions(artifacts({ source: 'real', account_assets: { positions: [] } }), 'account_snapshot')).toEqual([])
  expect(snapshotPositions(artifacts({ account_assets: { positions: [{ symbol: 'rb2610' }] } }), 'account_snapshot')).toBeNull()
  expect(quantityUnit({ quantity_kind: 'base_asset', quantity_label: '单位', quantity_max_decimals: 6, price_label: '', notional_label: '' }, 'ASSETCNY', 'CNY')).toBe('')
})

test('历史记录标题直接展示已保存的原因', () => {
  expect(executionState(row({ record: { ...row().record, raw_result: { status: 'BLOCKED', error: 'CLOSED' } } }))).toBe('未执行')
})
