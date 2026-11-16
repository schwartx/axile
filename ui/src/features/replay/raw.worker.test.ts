/// <reference types="vite/client" />
import { expect, mock, test } from 'bun:test'

mock.module('./raw-wasm/axile_replay_heat', () => ({
  default: async () => ({ memory: { buffer: new ArrayBuffer(65536) } }),
  RawBlock: class { head() { return undefined } step() { return '{"done":true,"events":[]}' } free() {} },
  BusinessBatch: class { constructor(input: string) { if (JSON.parse(input).subjects.length === 32) throw Error('REPLAY_BATCH_STATE_BUDGET') } results() { throw Error('REPLAY_BATCH_RESULT_BUDGET') } free() {} },
  BusinessDerivation: class { freed = false; needed() { return true } results() { if (this.freed) throw Error('freed'); return '{"quantity":23}' } free() { this.freed = true } },
  IntervalAnalysis: class { feed() {} begin_second() {} results() { return '{}' } free() {} },
}))

test('a mixed hit/miss page pins all hits before cache eviction, in preview and business reads', async () => {
  const originalSelf = globalThis.self
  const replies: Record<string, unknown>[] = []
  const scope = { onmessage: undefined as undefined | ((event: { data: Record<string, unknown> }) => Promise<void>), postMessage: (data: Record<string, unknown>) => replies.push(data) }
  globalThis.self = scope as unknown as typeof self
  try {
    await import('./raw.worker')
    await scope.onmessage!({ data: { kind: 'init', wasm: new ArrayBuffer(0) } })
    expect(replies.pop()).toEqual({ kind: 'initialized' })
    let id = 0
    const send = async (data: Record<string, unknown>) => {
      await scope.onmessage!({ data: { ...data, id: ++id } })
      const response = replies.pop()!
      expect(response.error).toBeUndefined()
      expect(response.cache_bytes as number).toBeLessThanOrEqual(4 * 1024 * 1024)
      return response
    }
    const row = async (n: number) => {
      const bytes = new Uint8Array(512 * 1024).fill(n).buffer
      const sha256 = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(b => b.toString(16).padStart(2, '0')).join('')
      return { key: `R:${n}`, bytes, block: { id: n + 1, length: bytes.byteLength, raw_length: 1, sha256, first_seq: 1, last_seq: 1 } }
    }
    const rows = await Promise.all(Array.from({ length: 10 }, (_, i) => row(i)))
    const decode = (n: number) => send({ kind: 'decode', key: rows[n].key, block: rows[n].block, sequences: [], quote_seq: null })
    for (let n = 0; n < 8; n += 2) {
      await send({ kind: 'cache', rows: rows.slice(n, n + 2) })
      await decode(n); await decode(n + 1)
    }
    await send({ kind: 'cache', rows: [rows[8], { key: rows[0].key, block: rows[0].block }] })
    expect((await send({ kind: 'has', keys: ['R:0', 'R:8', 'R:1'] })).present).toEqual([true, true, false])
    await decode(8); await decode(0)
    await send({kind:'pin-root',keys:['R:2']})
    await send({ kind: 'business-begin', recording: 'R', through_seq: 1, cutoff_seq: 1, subjects: [], streams: [1] })
    await send({ kind: 'business-page', stream: 1, rows: [rows[9], { key: rows[2].key, block: rows[2].block }], more: false })
    expect((await send({ kind: 'has', keys: ['R:2', 'R:9', 'R:3'] })).present).toEqual([true, true, false])
    await send({ kind: 'derive-begin', mode: 'statistics', targets: [] })
    await send({ kind: 'business-begin', recording: 'R', through_seq: 1, cutoff_seq: 1, subjects: Array.from({ length: 16 }, (_, i) => ({ id: i + 1, key: `T${i}`, kind: 'trade' })), streams: [] })
    expect((await send({ kind: 'business-result' })).split).toBeTrue()
    expect((await send({ kind: 'business-begin', recording: 'R', through_seq: 1, cutoff_seq: 1, subjects: Array.from({ length: 32 }, (_, i) => ({ id: i + 1, key: `T${i}`, kind: 'trade' })), streams: [] })).split).toBeTrue()
    expect((await send({ kind: 'derive-result', mode: 'statistics' })).result).toEqual({ quantity: 23 })
    await send({ kind:'business-begin',recording:'R',through_seq:1,cutoff_seq:1,subjects:[{id:1,kind:'trade',key:'T'}],streams:[] })
    expect((await send({kind:'business-step'})).needLinks).toBeTrue()
    await send({kind:'links',links:[],more:false})
    expect((await send({kind:'business-step'})).done).toBeTrue()
  } finally { globalThis.self = originalSelf }
})
