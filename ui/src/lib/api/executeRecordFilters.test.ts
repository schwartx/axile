import { expect, test } from 'bun:test'
import { getCachedExecuteRecords, getExecuteRecordDetail, getExecuteRecords } from './accounts'

test('precise record filters stay out of the account history cache', async () => {
  const original = globalThis.fetch
  const urls: string[] = []
  globalThis.fetch = ((input: string) => {
    urls.push(input)
    return Promise.resolve(Response.json({ count: input.includes('execution_id=') ? 1 : 602, data: [] }))
  }) as typeof fetch
  try {
    const history = await getExecuteRecords(97007, { limit: 500 })
    const selected = await getExecuteRecords(97007, { limit: 500, execution_id: 'old-execution', record_id: 1 })
    expect(selected.count).toBe(1)
    expect(getCachedExecuteRecords(97007)).toBe(history)
    expect(urls[1]).toContain('execution_id=old-execution&record_id=1')
  } finally { globalThis.fetch = original }
})

test('detail reads are explicit and preserve absent, null, empty and zero facts outside list cache', async () => {
  const original = globalThis.fetch
  const calls: { url: unknown; init?: RequestInit }[] = []
  const detail = {
    id: 42, account_id: 97008, execution_id: null, created_at: '2026-10-20T09:00:00', is_success: 0,
    input: null,
    result: {
      status: 'PARTIAL', account_assets: null,
      symbol_results: { A: { target_volume: 0, orders: [], trades: [{ trade_id: '', trade_value: 0, extra: { commission: -0.25 } }] } },
    },
  }
  globalThis.fetch = Object.assign((url: unknown, init?: RequestInit) => {
    calls.push({ url, init })
    return Promise.resolve(Response.json(detail))
  }, { preconnect: original.preconnect }) as typeof fetch
  try {
    const controller = new AbortController()
    const result = await getExecuteRecordDetail(97008, 42, controller.signal)
    expect(calls).toHaveLength(1)
    expect(calls[0]?.url).toBe('/api/v1/account/execute_records/97008/42')
    expect(calls[0]?.init?.signal).toBe(controller.signal)
    expect(result).toEqual(detail)
    expect(result.result).not.toHaveProperty('error')
    expect(result).not.toHaveProperty('raw_input')
    expect(result).not.toHaveProperty('raw_result')
    expect(getCachedExecuteRecords(97008)).toBeUndefined()
  } finally { globalThis.fetch = original }
})

test('detail preserves historical scalar, null and array roots without synthesizing objects', async () => {
  const original = globalThis.fetch
  const roots = [null, [], ['evidence'], false, 0, '', 'interrupted']
  let nextRoot = 0
  globalThis.fetch = Object.assign(() => Promise.resolve(Response.json({
    id: 43, account_id: 97009, execution_id: null, created_at: '2026-10-20T09:00:00', is_success: 0,
    input: roots[nextRoot], result: roots[nextRoot++],
  })), { preconnect: original.preconnect }) as typeof fetch
  try {
    for (const root of roots) {
      const detail = await getExecuteRecordDetail(97009, 43)
      expect(detail.input).toEqual(root)
      expect(detail.result).toEqual(root)
    }
  } finally { globalThis.fetch = original }
})
