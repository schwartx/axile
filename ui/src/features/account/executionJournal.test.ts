import { describe, expect, it } from 'bun:test'
import { activityWindowQuery, journalExecutions, journalWindow, loadJournal, matchesJournalStatus } from '@/features/account/executionJournal'
import { summarizeCosts } from '@/features/history/costs'
import type { AccountActivity, ActivityExecutionRecord } from '@/lib/api/accounts'

const empty = summarizeCosts([])
function execution(id: number, extra: Partial<ActivityExecutionRecord> = {}): AccountActivity {
  return { kind: 'execution', occurred_at: '2026-09-07T10:00:00', record: {
    id, execution_id: `exec-${id}`, created_at: '2026-09-07T10:00:00', is_success: 1,
    status: 'SUCCEEDED', symbol_results: {}, summary: empty, duration_sec: null, trade_count: 0, ...extra,
  } }
}
const window = journalWindow('custom', '2026-09-01', '2026-09-07')!

it('休市记录仍可通过跳过和未到位筛选，失败筛选不会收进休市', () => {
  const skipped = { status: '休市跳过' }
  const partial = { status: '部分品种休市 · 执行未全部完成' }
  expect(matchesJournalStatus(skipped, '已跳过')).toBe(true)
  expect(matchesJournalStatus(partial, '执行不到位')).toBe(true)
  expect(matchesJournalStatus(skipped, '执行失败')).toBe(false)
  expect(matchesJournalStatus(partial, '执行失败')).toBe(false)
})

describe('journal pagination', () => {
  it('loads beyond 500 records without folding individual executions', async () => {
    const data = Array.from({ length: 503 }, (_, i) => execution(i))
    const offsets: number[] = []
    const loaded = await loadJournal(1, window, new AbortController().signal, async (skip) => {
      offsets.push(skip)
      return { data: data.slice(skip, skip + 500), count: data.length }
    })
    expect(offsets).toEqual([0, 500])
    expect(journalExecutions(loaded)).toHaveLength(503)
  })
  it('does not publish partial results when a subsequent page fails', async () => {
    await expect(loadJournal(1, window, new AbortController().signal, async (skip) => {
      if (skip) throw new Error('offline')
      return { data: [execution(1)], count: 2 }
    })).rejects.toThrow('offline')
  })
  it('rejects shifting pagination and duplicate records', async () => {
    await expect(loadJournal(1, window, new AbortController().signal, async (skip) => ({ data: [execution(skip)], count: skip ? 3 : 2 }))).rejects.toThrow('发生变化')
    await expect(loadJournal(1, window, new AbortController().signal, async () => ({ data: [execution(1)], count: 2 }))).rejects.toThrow('重叠')
  })
  it('stops at the lower bound and excludes the day after the end', async () => {
    const a = execution(1)
    const loaded = await loadJournal(1, window, new AbortController().signal, async () => ({
      data: [{ ...a, occurred_at: '2026-09-08T00:00:00' }, { ...execution(2), occurred_at: '2026-09-07T23:59:59' }, { ...execution(3), occurred_at: '2026-08-31T23:59:59' }], count: 100,
    }))
    expect(loaded).toHaveLength(1)
  })
})

describe('journal quality', () => {
  it('keeps failed, terminated, noop and schedule skip rows individually with detail identities', () => {
    const rows = journalExecutions([
      execution(1), execution(2, { task_status: 'TERMINATED' }),
      execution(3, { is_success: 0, status: 'FAILED' }),
      { kind: 'schedule_skip', id: 5, occurred_at: '2026-09-07T10:00:00', channel: 'tq', reason_code: 'CALENDAR.CLOSED', calendar_day: '2026-09-07', calendar_id: '', calendar_label: '' },
    ])
    expect(rows.map((r) => r.status)).toEqual(['调仓完成', '执行已终止', '执行失败', '已跳过'])
    expect(rows[0].executionId).toBe('exec-1')
    expect(rows[3].executionId).toBeNull()
  })
  it('does not label a NOOP clear as a completed fill', () => {
    const row = journalExecutions([execution(1, { status: 'NOOP', execution_kind: 'clear_positions' })])[0]
    expect(row.status).toBe('无需清仓')
    expect(row.description).toBe('清仓 · 未记录成交')
  })
})

it('uses the list summary when the activity payload has no fill JSON', () => {
  const summary = { value: 500, cost: 1, lossBp: 2, coverage: 1, covered: 1, count: 1, amountComplete: true, fees: {}, feeCovered: 0 }
  const row = journalExecutions([execution(9, {
    status: 'SUCCEEDED', symbol_results: { A: { status: 'SUCCEEDED' } }, summary, duration_sec: 1.5, trade_count: 4,
  })])[0]
  expect(row.trades).toEqual([])
  expect(row.summary).toEqual(summary)
  expect(row.durationSec).toBe(1.5)
  expect(row.description).toBe('调仓 · 涉及 1 个品种 · 4 笔成交')
})

it('encodes the journal window as Shanghai naive since/until', () => {
  const window = journalWindow('custom', '2026-09-01', '2026-09-07')!
  expect(activityWindowQuery(window)).toEqual({ since: '2026-09-01T00:00:00', until: '2026-09-08T00:00:00' })
})

it('uses today for relative ranges and rejects reversed custom dates', () => {
  const w = journalWindow('7', '', '', new Date('2026-09-07T12:00:00'))!
  expect(new Date(w.start).toISOString()).toBe('2026-08-31T16:00:00.000Z')
  expect(new Date(w.end).toISOString()).toBe('2026-09-07T16:00:00.000Z')
  expect(journalWindow('custom', '2026-09-08', '2026-09-07')).toBeNull()
})

it('rejects overflow calendar dates', () => {
  expect(journalWindow('custom', '2026-02-30', '2026-03-01')).toBeNull()
})
