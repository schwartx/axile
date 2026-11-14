import { describe, expect, spyOn, test } from 'bun:test'
import { loadAnalysisInputs, type LocalInputs } from './localAnalysis'

const page = (next_cursor: string | null, record_id = '1') => ({ protocol: 2 as const, kind: 'costs' as const, algorithm: 'local-analysis-1' as const, account_id: 7, version: '12', range: '30' as const,
  initial_equity: 100, records: [{ record_id, observation: null, trades: [] }], next_cursor })

describe('complete versioned local inputs', () => {
  test('publishes only after the final page, preserves cursor and large record identities', async () => {
    const cursors: (string | null)[] = []
    const result = await loadAnalysisInputs(7, '12', '30', 'costs', new AbortController().signal, {}, async cursor => {
      cursors.push(cursor)
      return cursor ? page(null, '9223372036854775807') : page('next')
    })
    expect(cursors).toEqual([null, 'next'])
    expect(result.records.map(r => r.record_id)).toEqual(['1', '9223372036854775807'])
  })
  test('rejects a revision change, a cursor loop and an interrupted load', async () => {
    await expect(loadAnalysisInputs(7, '12', '30', 'costs', new AbortController().signal, {}, async cursor => ({ ...page(cursor ? null : 'next'), version: cursor ? '13' : '12' }))).rejects.toThrow('版本')
    await expect(loadAnalysisInputs(7, '12', '30', 'costs', new AbortController().signal, {}, async () => page('same'))).rejects.toThrow('未前进')
    const controller = new AbortController()
    await expect(loadAnalysisInputs(7, '12', '30', 'costs', controller.signal, {}, async () => { controller.abort(); return page(null) })).rejects.toThrow('Aborted')
  })
  test('rejects oversized windows without returning a partial result', async () => {
    await expect(loadAnalysisInputs(7, '12', '30', 'costs', new AbortController().signal, {}, async () => ({ ...page(null), records: Array.from({ length: 20_001 }, (_, i) => ({ record_id: String(i), observation: null, trades: [] })) }))).rejects.toThrow('上限')
  })
  test('cost pages cannot be reused as fee observations', async () => {
    await expect(loadAnalysisInputs(7, '12', '30', 'fees', new AbortController().signal, {}, async () => page(null))).rejects.toThrow('版本')
  })
})

// Retain the public protocol type when transport fields are added.
const _input: LocalInputs = page(null)
void _input

test('decodes compact scoped trades without losing unknown values or record identity', async () => {
  const columns = ['symbol', 'time_us', 'day', 'estimated', 'side', 'price', 'quantity', 'multiplier', 'reference', 'fee', 'fee_currency']
  const selection = { day: '2026-01-01' }
  const result = await loadAnalysisInputs(7, '12', '30', 'costs', new AbortController().signal, selection, async () => ({
    ...page(null), selection, records: [{ record_id: '9223372036854775807', observation: null, trades: { columns, rows: [['A', 1000, selection.day, true, 'none', 100, 2, 1, null, 0, 'CNY']] } }],
  }))
  expect(result.records[0].record_id).toBe('9223372036854775807')
  expect(result.records[0].trades).toEqual([{ symbol: 'A', time_us: 1000, day: selection.day, estimated: true, side: 'none', price: 100, quantity: 2, multiplier: 1, reference: null, fee: 0, fee_currency: 'CNY' }])
  await expect(loadAnalysisInputs(7, '12', '30', 'costs', new AbortController().signal, selection, async () => page(null))).rejects.toThrow('选区')
  await expect(loadAnalysisInputs(7, '12', '30', 'costs', new AbortController().signal, {}, async () => ({ ...page(null), records: [{ record_id: '1', observation: null, trades: { columns, rows: [['A']] } }] }))).rejects.toThrow('行长度')
})

test('sends the selected range to the production API without undefined selectors', async () => {
  const selection = { day: '2026-01-01', record_id: undefined }
  const request = spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...page(null), selection })))
  try {
    await loadAnalysisInputs(7, '12', '30', 'costs', new AbortController().signal, selection)
    const url = new URL(String(request.mock.calls[0][0]), 'http://localhost')
    expect(Object.fromEntries(url.searchParams)).toEqual({ version: '12', range: '30', kind: 'costs', day: '2026-01-01' })
  } finally { request.mockRestore() }
})
