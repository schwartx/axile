import { expect, test } from 'bun:test'
import { parseJournalScope, performanceJournalPath, loadCostGroups, compareJournal, journalTotals, snapshotExecution, filterLiveExecutions } from '@/features/account/journalSource'
import { summarizeCosts } from '@/features/history/costs'
import { journalExecutions } from '@/features/account/executionJournal'
import type { CostPage } from '@/lib/api/performance'
import type { ExecuteRecord } from '@/types/api'

const record: ExecuteRecord = { id: 5, account_id: 2, execution_id: 'e5', created_at: '2026-09-07T17:00:00', is_success: 1, input: null, result: { symbol_results: {
  A: { sizing: { unit_multiplier: 10 }, first_tick: { bid_price: 99, ask_price: 101 }, orders: [{ order_id: '1', direction: 'buy' }], trades: [{ order_id: '1', trade_price: 101, trade_volume: 2 }] },
  B: { sizing: { unit_multiplier: 10 }, first_tick: { last_price: 100 }, trades: [{ trade_price: 99, trade_volume: 1, extra: { direction: 'sell' } }, { trade_price: 99, trade_volume: 1 }] },
  C: { trades: [{ trade_price: 10, trade_volume: 1 }] },
} } }

test('chart interval link round-trips exact milliseconds without expanding to a calendar day', () => {
  const selection = { kind: 'interval' as const, start: 1788752400001, end: 1788752442003 }
  const url = performanceJournalPath(2, 'snapshot-1', '90', selection)
  const { scope, error } = parseJournalScope(new URL(url, 'http://localhost').searchParams)
  expect(error).toBeNull()
  expect(scope).toEqual({ version: 'snapshot-1', range: '90', start: selection.start, end: selection.end })
  expect(parseJournalScope(new URL(performanceJournalPath(2, 's', 'all', { kind: 'execution', recordId: 5, time: 100 }), 'http://localhost').searchParams).scope?.record_id).toBe(5)
})

test('invalid or mixed snapshot filters never fall back to live activity', () => {
  for (const suffix of ['start=1', 'start=2&end=1', 'start=NaN&end=3', 'start=1&end=2&day=2026-09-07', 'day=2026-02-30', 'record_id=0', 'record_id=1&day=2026-09-07', 'start=&end=2']) {
    expect(parseJournalScope(new URLSearchParams(`version=s&performance_range=all&${suffix}`)).error).not.toBeNull()
  }
  expect(parseJournalScope(new URLSearchParams('start=1&end=2')).error).not.toBeNull()
  expect(parseJournalScope(new URLSearchParams('range=30')).scope).toBeNull()
})

test('live keyword filter keeps whole-execution summaries and matches symbol names', () => {
  const summary = { ...summarizeCosts([]), value: 30, cost: 30, count: 4, amountComplete: false }
  const rows = journalExecutions([{
    kind: 'execution', occurred_at: record.created_at, record: {
      id: 5, execution_id: 'e5', created_at: record.created_at, is_success: 1, status: 'SUCCEEDED',
      symbol_results: { A: { status: 'SUCCEEDED' }, B: { status: 'SUCCEEDED' }, C: { status: 'SUCCEEDED' } },
      summary, duration_sec: null, trade_count: 4,
    },
  }])
  expect(filterLiveExecutions(rows, 'b')).toEqual(rows)
  expect(filterLiveExecutions(rows, 'z')).toEqual([])
  expect(rows[0].summary).toEqual(summary)
})

test('cost sorting puts unknown last, improvements after losses and ties in stable order', () => {
  const empty = summarizeCosts([])
  const rows = [{ key: 'unknown', summary: empty }, { key: 'better', summary: { ...empty, cost: -1 } }, { key: 'worse', summary: { ...empty, cost: 2 } }]
  expect(rows.toSorted((a, b) => compareJournal(a, b, 'cost')).map(r => r.key)).toEqual(['worse', 'better', 'unknown'])
})

test('未知活动汇总保留 null，不当作无成交加入总计', () => {
  const rows = journalExecutions([{
    kind: 'execution', occurred_at: record.created_at, record: {
      id: 6, execution_id: 'e6', created_at: record.created_at, is_success: 1, status: 'SUCCEEDED',
      symbol_results: {}, summary: null, duration_sec: null, trade_count: null,
    },
  }])
  expect(rows[0].summary).toBeNull()
  expect(rows[0].description).toBe('调仓 · 成交信息未获取')
  expect(journalTotals(rows)).toBeNull()
  expect(journalTotals([])?.count).toBe(0)
  const known = { key: 'known', summary: { ...summarizeCosts([]), value: 10 } }
  expect([rows[0], known].toSorted((a, b) => compareJournal(a, b, 'value'))[0].key).toBe('known')
})

test('lightweight pagination completes beyond one page and fails closed on broken cursors', async () => {
  const base: CostPage<number> = { version: 's', count: 3, summary: { ...summarizeCosts([]), estimated: 0 }, successful: 0, noop: 0, data: [], next_cursor: null }
  const query = { version: 's', range: 'all' as const, dimension: 'execution' as const }
  const signal = new AbortController().signal
  const page = await loadCostGroups(2, query, signal, async q => q.cursor ? { ...base, data: [3] } : { ...base, data: [1, 2], next_cursor: 'next' })
  expect(page.data).toEqual([1, 2, 3])
  await expect(loadCostGroups(2, query, signal, async () => ({ ...base, data: [1], next_cursor: 'repeat' }))).rejects.toThrow('分页发生重叠')
  await expect(loadCostGroups(2, query, signal, async () => ({ ...base, data: [1] }))).rejects.toThrow('分页不完整')
  await expect(loadCostGroups(2, query, signal, async () => ({ ...base, version: 'new' }))).rejects.toThrow('范围发生变化')
  await expect(loadCostGroups(2, query, signal, async q => { if (q.cursor) throw new Error('offline'); return { ...base, data: [1], next_cursor: 'next' } })).rejects.toThrow('offline')
})

test('snapshot execution status preserves blocked and partial outcomes before is_success', () => {
  const base = { key: '1', record: { id: 1, execution_id: 'e1', created_at: record.created_at, is_success: 0, raw_result: { status: 'BLOCKED' } }, noop: false, symbolCount: 0, summary: summarizeCosts([]) }
  expect(snapshotExecution(base).status).toBe('未执行')
  expect(snapshotExecution({ ...base, record: { ...base.record, raw_result: { status: 'BLOCKED', reason_code: 'COMMON.SESSION.CLOSED' } } }).status).toBe('休市跳过')
  expect(snapshotExecution({ ...base, record: { ...base.record, raw_result: { status: 'PARTIAL' } } }).status).toBe('执行未全部完成')
})

test('裁剪后的绩效投影使用独立统计，成交分页不影响整次执行摘要', () => {
  const row = snapshotExecution({ key: '5', record: { id: 5, execution_id: record.execution_id, created_at: record.created_at, is_success: 1, raw_result: { status: 'SUCCEEDED', symbol_results: { A: { status: 'SUCCEEDED' }, B: { status: 'SUCCEEDED' } } } }, tradeCount: 1000, symbolCount: 1, noop: false, summary: summarizeCosts([]) })
  expect(row.description).toBe('调仓 · 涉及 2 个品种 · 1000 笔成交')
  expect(row.trades).toEqual([])
})

test('普通列表与快照列表摘要显示执行规模，状态列单独显示结论', () => {
  const live = journalExecutions([{
    kind: 'execution', occurred_at: record.created_at, record: {
      id: 5, execution_id: 'e5', created_at: record.created_at, is_success: 1, status: 'SUCCEEDED',
      symbol_results: { A: { status: 'SUCCEEDED' }, B: { status: 'SUCCEEDED' }, C: { status: 'SUCCEEDED' } },
      summary: summarizeCosts([]), duration_sec: null, trade_count: 4,
    },
  }])[0]
  const snapshot = snapshotExecution({
    key: '5',
    record: { id: 5, execution_id: 'e5', created_at: record.created_at, is_success: 1, raw_result: { status: 'SUCCEEDED', symbol_results: { A: { status: 'SUCCEEDED' }, B: { status: 'SUCCEEDED' }, C: { status: 'SUCCEEDED' } } } },
    noop: false, symbolCount: 99, summary: summarizeCosts([]), tradeCount: 4, reason: '调仓完成',
  })
  for (const row of [live, snapshot]) {
    expect(row.description).toBe('调仓 · 涉及 3 个品种 · 4 笔成交')
    expect(row.status).toBe('调仓完成')
  }
})
