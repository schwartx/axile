import { afterEach, expect, test } from 'bun:test'
import './analysisWorkerTestSupport'
import { ApiError } from '@/lib/api/client'
import type { AnalysisExecutionDetail, AnalysisTable } from '@/lib/api/performance'
import { decodeExecutionDetail, detailNextOffset, executionDetailEntry, loadExecutionDetail, mergeExecutionDetail } from './analysisExecutionDetail'

const table = (rows: Record<string, unknown>[], columns = Object.keys(rows[0] ?? {})): AnalysisTable => ({ columns, rows: rows.map(row => columns.map(column => row[column])) })
const frozenSummary = { value: null, cost: null, loss_bp: null, coverage: null, covered: 0, count: 0, amount_complete: true, fee_covered: 0, fees: {} }
function detail(snapshot = 's1', record = 7): AnalysisExecutionDetail {
  return { schema_version: 1, snapshot_id: snapshot, record_id: record,
    header: { execution_id: 'exec7', created_at: '2026-10-20T10:00:00', is_success: 1, noop: false, reason: null, raw_present: 1, raw_status: 'SUCCEEDED', has_symbol_results: true, positions_present: true },
    summary: frozenSummary, tables: {
      positions: table([], ['idx', 'symbol', 'quantity', 'direction']), attempts: table([], ['idx', 'symbol', 'planned', 'filled', 'reason']),
      transactions: table([], ['idx', 'symbol', 'quantity']), transaction_fees: table([], ['transaction_idx', 'currency', 'value']), symbols: table([], ['symbol', 'present']),
    }, next_offset: null, total: 0, next_offsets: { positions: null, attempts: null, symbols: null },
  }
}
const originalFetch = globalThis.fetch
function fetchWith(implementation: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>) {
  globalThis.fetch = Object.assign(implementation, { preconnect: originalFetch.preconnect })
}
afterEach(() => { globalThis.fetch = originalFetch })

test('positions presence and page completeness distinguish unknown, empty and not fully loaded', () => {
  const payload = detail()
  expect(decodeExecutionDetail(payload).positions).toEqual([])
  payload.header.positions_present = false
  expect(decodeExecutionDetail(payload).positions).toBeNull()
  payload.header.positions_present = true
  payload.next_offsets = { positions: 200 }
  const partial = decodeExecutionDetail(payload)
  expect(partial.positions).toEqual([])
  expect(partial.positionsComplete).toBe(false)
  expect(partial.nextOffset).toBe(200)
})

test('single-record decoding retains raw presence, nullable quantities and real zero or negative fees', () => {
  const payload = detail()
  payload.header.raw_present = (1 << 0) | (1 << 2)
  payload.header.raw_error = null
  payload.header.raw_task_status = 'TERMINATED'
  payload.tables.positions = table([{ idx: 0, symbol: 'rb', quantity: null, direction: null }])
  payload.tables.symbols = table([{ symbol: 'rb', present: 3, status: 'PARTIAL', error: null, reason_code: 'must-not-leak' }])
  payload.tables.transactions = table([{ idx: 4, symbol: 'rb', side: null, quantity: null, price: 0, reference: null, reference_source: null, time: null, end_time: null, before: 0, target: null, time_estimated: false, ...frozenSummary }])
  payload.tables.transaction_fees = table([{ transaction_idx: 4, currency: 'CNY', value: 0 }, { transaction_idx: 4, currency: 'USD', value: -2 }])
  payload.summary = { ...frozenSummary, fees: { CNY: 0, USD: -2 } }
  payload.total = 1
  const decoded = decodeExecutionDetail(payload)
  expect(decoded.positions?.[0].quantity).toBeNull()
  expect(decoded.rawResult).toEqual({ status: 'SUCCEEDED', error: null, symbol_results: { rb: { status: 'PARTIAL', error: null } } })
  expect(decoded.transactions[0]).toMatchObject({ quantity: null, price: 0, referenceSource: null, before: 0, time: null, summary: { fees: { CNY: 0, USD: -2 } } })
  expect(decoded.summary.fees).toEqual({ CNY: 0, USD: -2 })
})

test('independent detail page offsets remain visible and table merging never duplicates transactions or fees', () => {
  const first = detail(), page = detail()
  first.next_offset = 200
  first.next_offsets = { positions: 200, attempts: 400 }
  first.tables.transactions = table([{ idx: 0, symbol: 'rb', quantity: 0 }])
  first.tables.transaction_fees = table([{ transaction_idx: 0, currency: 'CNY', value: 0 }])
  page.tables.transactions = table([{ idx: 0, symbol: 'rb', quantity: 0 }, { idx: 1, symbol: 'cu', quantity: 2 }])
  page.tables.transaction_fees = table([{ transaction_idx: 0, currency: 'CNY', value: 0 }, { transaction_idx: 1, currency: 'USD', value: -1 }])
  page.next_offsets = { positions: 400, attempts: 400 }
  page.total = 2
  const merged = mergeExecutionDetail(first, page)
  expect(merged.tables.transactions.rows).toHaveLength(2)
  expect(merged.tables.transaction_fees.rows).toHaveLength(2)
  expect(detailNextOffset(merged, 200)).toBe(400)
  expect(merged.summary).toEqual(frozenSummary)
  expect(() => detailNextOffset({ ...page, next_offset: 200 }, 200)).toThrow('未向前推进')
  expect(() => mergeExecutionDetail(first, detail('s2'))).toThrow('版本不匹配')
})

test('concurrent detail readers share a GET and cache identity includes account, snapshot and record', async () => {
  let calls = 0
  let release!: () => void
  fetchWith(async () => { calls++; await new Promise<void>(resolve => { release = resolve }); return Response.json(detail('cache', 101)) })
  const entry = executionDetailEntry(45, 'cache', 101)
  const first = loadExecutionDetail(entry), second = loadExecutionDetail(entry)
  expect(first).toBe(second)
  release(); await first
  expect(calls).toBe(1)
  await loadExecutionDetail(entry)
  expect(calls).toBe(1)
  expect(entry.state.data?.recordId).toBe(101)
  expect(executionDetailEntry(45, 'other', 101).state.data).toBeNull()
  expect(executionDetailEntry(46, 'cache', 101).state.data).toBeNull()
  expect(executionDetailEntry(45, 'cache', 102).state.data).toBeNull()
})

test('a late old selection cannot replace another snapshot and canceled responses are discarded', async () => {
  let release!: () => void
  fetchWith(async () => { await new Promise<void>(resolve => { release = resolve }); return Response.json(detail('old', 103)) })
  const old = executionDetailEntry(47, 'old', 103), current = executionDetailEntry(47, 'new', 103)
  const request = loadExecutionDetail(old)
  old.controller?.abort()
  release(); await request
  expect(old.state.data).toBeNull()
  expect(old.state.loading).toBe(false)
  expect(current.state.data).toBeNull()
  fetchWith(async () => Response.json(detail('new', 103)))
  await loadExecutionDetail(current)
  expect(current.state.data?.recordId).toBe(103)
  expect(old.state.data).toBeNull()
})

test('an aborted shared request can restart for a new subscriber', async () => {
  let release!: () => void
  let calls = 0
  fetchWith(async () => {
    calls++
    if (calls === 1) await new Promise<void>(resolve => { release = resolve })
    return Response.json(detail('remount', 107))
  })
  const entry = executionDetailEntry(49, 'remount', 107)
  const oldRequest = loadExecutionDetail(entry)
  entry.controller?.abort()
  const newRequest = loadExecutionDetail(entry)
  release()
  await Promise.all([oldRequest, newRequest])
  expect(calls).toBe(2)
  expect(entry.state.data?.recordId).toBe(107)
})

test('expired snapshots remain explicit and never issue refresh or newest-snapshot requests', async () => {
  const paths: string[] = []
  fetchWith(async (input, init) => {
    paths.push(String(input))
    expect(init?.method).toBeUndefined()
    return Response.json({ detail: 'gone' }, { status: 410 })
  })
  const entry = executionDetailEntry(48, 'expired', 104)
  await loadExecutionDetail(entry)
  expect(paths).toHaveLength(1)
  expect(paths[0]).toContain('snapshot_id=expired')
  expect(entry.state.expired).toBe(true)
  expect(entry.state.error).toBeInstanceOf(ApiError)
  expect(entry.state.data).toBeNull()
})

test('server identity mismatch is rejected before publishing detail data', async () => {
  fetchWith(async () => Response.json(detail('wrong', 105)))
  const entry = executionDetailEntry(48, 'requested', 105)
  await loadExecutionDetail(entry)
  expect(entry.state.error?.message).toContain('版本不匹配')
  expect(entry.state.data).toBeNull()
})
