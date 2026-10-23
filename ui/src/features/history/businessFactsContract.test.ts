import { afterEach, expect, test } from 'bun:test'
import { ApiError } from '@/lib/api/client'
import type { AnalysisExecutionDetail, AnalysisTable } from '@/lib/api/performance'
import { executionDetailEntry, loadExecutionDetail } from '@/features/history/analysisExecutionDetail'
import { buildExecutionErrorInfo } from '@/features/history/executionErrorAdapter'
import { readExecutionRecordEvidence } from '@/features/history/executionRecordEvidence'
import type { ExecuteRecord } from '@/types/api'

const table = (columns: string[], rows: unknown[][] = []): AnalysisTable => ({ columns, rows })
const summary = { value: null, cost: null, loss_bp: null, coverage: null, covered: 0, count: 0, amount_complete: true, fee_covered: 0, fees: {} }

function page(snapshot: string, record: number, more = false): AnalysisExecutionDetail {
  return {
    schema_version: 1, snapshot_id: snapshot, record_id: record,
    header: { execution_id: null, created_at: null, is_success: null, noop: null, reason: null, raw_present: 0, has_symbol_results: false, positions_present: true },
    summary,
    tables: {
      positions: table(['idx', 'symbol', 'quantity', 'direction'], [[0, 'rb2701', 0, null]]),
      attempts: table(['idx', 'symbol', 'planned', 'filled', 'reason']),
      transactions: table(['idx', 'symbol', 'quantity']),
      transaction_fees: table(['transaction_idx', 'currency', 'value']),
      symbols: table(['symbol', 'present']),
    },
    next_offset: null, total: 0, next_offsets: { positions: more ? 200 : null, attempts: null, symbols: null },
  }
}

const originalFetch = globalThis.fetch
function fetchWith(implementation: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>) {
  globalThis.fetch = Object.assign(implementation, { preconnect: originalFetch.preconnect })
}
afterEach(() => { globalThis.fetch = originalFetch })

test('record identity opens typed full error evidence without manufacturing a legacy execution ID', async () => {
  const trace = 'Traceback\n  worker.py:27005\n' + '原始细节\n'.repeat(300) + '尾部根因'
  const record: ExecuteRecord = {
    id: 27005, account_id: 27005, execution_id: null, created_at: '2026-10-20T10:00:00', is_success: 0,
    status: 'FAILED', error: '柜台原始原因', input: { curr_target: { 'SSE/510300': 0 }, last_target: null },
    result: { status: 'FAILED', error: '柜台原始原因', technical_detail: trace },
  }
  const read = await readExecutionRecordEvidence(27005, { recordId: 27005 }, new AbortController().signal, {
    list: async () => { throw new Error('record identity must not query the execution-ID list') },
    detail: async (accountId, recordId) => {
      expect([accountId, recordId]).toEqual([27005, 27005])
      return record
    },
  })
  expect(read).toBe(record)
  const input = read?.input
  if (input == null || typeof input !== 'object' || Array.isArray(input)) throw new Error('Expected an object input fixture')
  expect(input.curr_target).toEqual({ 'SSE/510300': 0 })
  const info = buildExecutionErrorInfo({ accountId: 27005, record: read })
  expect(info.details.some(item => item.value === trace)).toBe(true)
  expect(info.text).toContain(trace)
  expect(info.evidence).toContainEqual({ label: '记录 ID', value: '27005' })
  expect(info.evidence.some(item => item.label === 'execution_id')).toBe(false)
})

test('corrupt later facts page cannot partially replace published snapshot evidence', async () => {
  const paths: URL[] = []
  let calls = 0
  fetchWith(async input => {
    paths.push(new URL(String(input), 'http://localhost'))
    calls++
    const payload = page('facts-corrupt-page', 27001, calls === 1)
    if (calls === 2) payload.tables.positions = table(['idx', 'symbol', 'quantity', 'direction'], [[1, 'cu2701', 'invalid', null]])
    if (calls === 3) payload.tables.positions.rows = [[1, 'cu2701', null, null]]
    return Response.json(payload)
  })
  const entry = executionDetailEntry(27001, 'facts-corrupt-page', 27001)
  await loadExecutionDetail(entry)
  const publishedData = entry.state.data
  const publishedPayload = entry.payload
  expect(publishedData?.positions).toEqual([{ idx: 0, symbol: 'rb2701', quantity: 0, direction: null }])
  await loadExecutionDetail(entry, true)
  expect(entry.state.data).toBe(publishedData)
  expect(entry.payload).toBe(publishedPayload)
  expect(entry.state.error?.message).toContain('数值无效')
  await loadExecutionDetail(entry, true)
  expect(entry.state.error).toBeNull()
  expect(entry.state.data?.positions).toEqual([
    { idx: 0, symbol: 'rb2701', quantity: 0, direction: null },
    { idx: 1, symbol: 'cu2701', quantity: null, direction: null },
  ])
  expect(paths.map(path => path.searchParams.get('offset'))).toEqual(['0', '200', '200'])
  expect(paths.every(path => path.searchParams.get('snapshot_id') === 'facts-corrupt-page' && path.searchParams.get('record_id') === '27001')).toBe(true)
})

test('snapshot expiry after a first page preserves evidence and blocks further load-more requests', async () => {
  const paths: string[] = []
  fetchWith(async (input, init) => {
    expect(init?.method).toBeUndefined()
    paths.push(String(input))
    return paths.length === 1 ? Response.json(page('facts-expired-page', 27002, true)) : Response.json({ detail: 'snapshot expired' }, { status: 410 })
  })
  const entry = executionDetailEntry(27002, 'facts-expired-page', 27002)
  await loadExecutionDetail(entry)
  const publishedData = entry.state.data
  const publishedPayload = entry.payload
  await loadExecutionDetail(entry, true)
  expect(entry.state.data).toBe(publishedData)
  expect(entry.payload).toBe(publishedPayload)
  expect(entry.state.expired).toBe(true)
  expect(entry.state.error).toBeInstanceOf(ApiError)
  await loadExecutionDetail(entry, true)
  expect(paths).toHaveLength(2)
  expect(paths.every(path => path.includes('snapshot_id=facts-expired-page') && path.includes('record_id=27002'))).toBe(true)
})

test('late failure for another account cannot invalidate equal snapshot and record identities', async () => {
  const paths: string[] = []
  let release!: () => void
  fetchWith(async input => {
    const path = String(input)
    paths.push(path)
    if (path.includes('/account/performance/27003/')) {
      await new Promise<void>(resolve => { release = resolve })
      return Response.json({ detail: 'record unavailable in this account' }, { status: 404 })
    }
    return Response.json(page('facts-account-scope', 27003))
  })
  const failed = executionDetailEntry(27003, 'facts-account-scope', 27003)
  const valid = executionDetailEntry(27004, 'facts-account-scope', 27003)
  const late = loadExecutionDetail(failed)
  await loadExecutionDetail(valid)
  const published = valid.state.data
  expect(published?.recordId).toBe(27003)
  release()
  await late
  expect(paths).toHaveLength(2)
  expect(paths.some(path => path.includes('/account/performance/27003/'))).toBe(true)
  expect(paths.some(path => path.includes('/account/performance/27004/'))).toBe(true)
  expect(failed.state.data).toBeNull()
  expect(failed.state.error).toBeInstanceOf(ApiError)
  expect(valid.state.data).toBe(published)
  expect(valid.state.error).toBeNull()
})
