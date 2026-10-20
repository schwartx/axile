import { expect, test } from 'bun:test'
import { buildExecutionErrorInfo, readEvidencePages, catalogEvidence } from './executionErrorAdapter'
import type { ExecuteRecord, ExecutionEvent } from '@/types/api'

const raw = 'long '.repeat(1000) + '\npassword=synthetic token=synthetic api_key=synthetic\n尾部根因'
const record: ExecuteRecord = { account_id: 1, id: 1, execution_id: null, input: {}, result: { technical_detail: raw, traceback: 'Traceback\nline 123\n' + raw }, is_success: 0, created_at: '' }

test('typed record detail preserves complete diagnostic text and existing traceback', () => {
  const info = buildExecutionErrorInfo({ accountId: 7, recordId: 1, record })
  expect(info.text).toContain(raw)
  expect(info.text).toContain('Traceback\nline 123\n')
  expect(info.evidence).toContainEqual({ label: '记录 ID', value: '1' })
})
test('zero record identity remains visible in error evidence', () => {
  expect(buildExecutionErrorInfo({ accountId: 7, record: { ...record, id: 0 } }).evidence)
    .toContainEqual({ label: '记录 ID', value: '0' })
})
test('non-object historical roots remain explicit evidence rather than empty mappings', () => {
  const roots: [ExecuteRecord['result'], string][] = [[null, 'null'], ['原始字符串', '原始字符串'], [0, '0'], [false, 'false'], [[], '[]'], [['原始项'], '[\n  "原始项"\n]']]
  for (const [value, expected] of roots) {
    const info = buildExecutionErrorInfo({ accountId: 7, record: { ...record, input: value, result: value } })
    expect(info.details).toContainEqual({ label: '记录 1 · 输入根值（非对象）', value: expected })
    expect(info.details).toContainEqual({ label: '记录 1 · 结果根值（非对象）', value: expected })
  }
})
test('canonical head and preserved result reasons keep separate provenance', () => {
  const info = buildExecutionErrorInfo({ accountId: 7, record: { ...record, error: '主记录原因', outcome_reason: '主记录结论原因', result: { error: '结果原文' } } })
  expect(info.details).toContainEqual({ label: '记录 1 · 主信息 · error', value: '主记录原因' })
  expect(info.details).toContainEqual({ label: '记录 1 · 主信息 · outcome_reason', value: '主记录结论原因' })
  expect(info.details).toContainEqual({ label: '记录 1 · 结果 · error', value: '结果原文' })
})
test('event-only catalog evidence preserves phase reason symbols and elapsed time', () => {
  const event = { id: 2, event_type: 'instruments_waiting', reason_code: 'CATALOG.PENDING', details: { message: raw, symbols: ['rb2701', 'au2701'], elapsed_seconds: 25 } } as unknown as ExecutionEvent
  const info = buildExecutionErrorInfo({ accountId: 7, executionId: 'e', events: [event] })
  expect(info.text).toContain('等待合约资料')
  expect(info.text).toContain('CATALOG.PENDING')
  expect(info.text).toContain('rb2701')
  expect(info.text).toContain('25 秒')
  expect(info.text).toContain(raw)
  expect(catalogEvidence({ ...event, event_type: 'instruments_failed' })?.label).toContain('合约资料失败')
})
test('missing historical originals are explicitly described', () => {
  expect(buildExecutionErrorInfo({ accountId: 7 }).text).toContain('历史未保存的内容无法补回')
})
test('all pages are read serially without a recent-history limit', async () => {
  const skips: number[] = []
  let active = 0
  const controller = new AbortController()
  const result = await readEvidencePages(async (skip, limit) => {
    expect(active++).toBe(0)
    await Promise.resolve()
    skips.push(skip)
    active--
    return { data: Array.from({ length: Math.min(limit, 1201 - skip) }, (_, index) => ({ id: skip + index })), count: 1201 }
  }, controller.signal)
  expect(skips).toEqual([0, 500, 1000])
  expect(result.complete).toBe(true)
  expect(result.data).toHaveLength(1201)
})
test('later-page failure retains earlier evidence and the failed range', async () => {
  const failure = new Error('second-page failed\n' + raw)
  const result = await readEvidencePages(async skip => {
    if (skip) throw failure
    return { data: [{ id: 1 }], count: 2 }
  }, new AbortController().signal)
  expect(result.data).toEqual([{ id: 1 }])
  expect(result.count).toBe(2)
  expect(result.complete).toBe(false)
  expect(result.error).toBe(failure)
})
test('cancellation invalidates partial data and overlapping pages cannot claim completeness', async () => {
  const controller = new AbortController()
  const pending = readEvidencePages(async () => { controller.abort(); return { data: [], count: 0 } }, controller.signal)
  await expect(pending).rejects.toThrow()
  const overlap = await readEvidencePages(async () => ({ data: [{ id: 1 }], count: 2 }), new AbortController().signal)
  expect(overlap.complete).toBe(false)
  expect(overlap.error?.message).toContain('分页重叠')
})

test('nested worker and event tracebacks keep actual lines as readable text', () => {
  const trace = 'Traceback\n  worker.py:123\n' + raw
  const event = { id: 3, event_type: 'execution_failed', details: { error: { message: raw, traceback: trace } } } as unknown as ExecutionEvent
  const info = buildExecutionErrorInfo({ accountId: 7, record: { ...record, result: { extra: { worker_error: { message: raw, traceback: trace } } } }, events: [event] })
  expect(info.text).toContain(trace)
  expect(info.details.some(item => item.value === trace)).toBe(true)
})

test('appends during pagination stay outside the count frozen by its first page', async () => {
  const page = await readEvidencePages(async skip => skip === 0 ? { data: [{ id: 1 }], count: 2 } : { data: [{ id: 2 }, { id: 3 }], count: 3 }, new AbortController().signal)
  expect(page.complete).toBe(true)
  expect(page.count).toBe(2)
  expect(page.data).toEqual([{ id: 1 }, { id: 2 }])
})
