import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import init, { AccountAnalysis, ReplayAnalysis } from '@/features/replay/heat-wasm/axile_replay_heat'
import { summarizeCosts, type CostTrade } from './costs'

await init({ module_or_path: readFileSync(new URL('../replay/heat-wasm/axile_replay_heat_bg.wasm', import.meta.url)) })

test('packaged WASM cost summaries match the independent existing JS oracle', () => {
  const trades: CostTrade[] = Array.from({ length: 1000 }, (_, i) => {
    const price = 100 + i % 13, quantity = 1 + i % 3, reference = i % 17 ? 100 : null
    const value = i % 19 ? price * quantity * 10 : null
    const side = i % 2 ? 'buy' as const : 'sell' as const
    const difference = (price - 100) * (side === 'buy' ? 1 : -1)
    return { symbol: i % 3 ? 'A' : 'B', day: '2026-01-01', time: i, timeEstimated: i % 5 === 0, side, price, quantity, reference, referenceSource: reference ? 'mid' : null,
      value, cost: reference != null && value != null ? difference * quantity * 10 : null, lossBp: reference != null ? difference / 100 * 1e4 : null, fee: i % 7 ? 0.1 : null, feeCurrency: i % 2 ? 'CNY' : 'USD' }
  })
  const kernel = new AccountAnalysis(JSON.stringify({ protocol: 2, kind: 'costs', algorithm: 'local-analysis-1', initial_equity: null, records: [{ record_id: '1', observation: null, trades: trades.map(t => ({ symbol: t.symbol, time_us: t.time * 1000, day: t.day, estimated: t.timeEstimated, side: t.side, price: t.price, quantity: t.quantity, reference: t.reference, multiplier: t.value != null ? 10 : null, fee: t.fee, fee_currency: t.feeCurrency })) }] }))
  try {
    for (const query of [{}, { start: 0, end: 999 }, { symbol: 'A', side: 'buy' }]) {
      const result = JSON.parse(kernel.query(JSON.stringify(query)))
      const selected = trades.filter(t => (!('start' in query) || t.time > query.start! && t.time <= query.end!) && (!('symbol' in query) || t.symbol === query.symbol && t.side === query.side))
      expect(result.summary).toEqual(summarizeCosts(selected))
      expect(result.histogram.reduce((a: number, b: number) => a + b, result.unknown)).toBe(selected.length)
    }
  } finally { kernel.free() }
})

test('fee preview keeps quantity mode, skip semantics, Shanghai closes and no opening fee', () => {
  const items = [
    { time: Date.parse('2026-01-01T15:59:59Z') * 1000, prices: { A: 100 }, target: { A: 1 }, target_units: { A: 10 }, target_quantities: { A: 10 } },
    { time: Date.parse('2026-01-01T16:00:00Z') * 1000, prices: {}, target: {}, target_units: {}, target_quantities: {} },
    { time: Date.parse('2026-01-01T16:00:01Z') * 1000, prices: { A: 110 }, target: {}, target_units: {}, target_quantities: {} },
  ]
  const kernel = new AccountAnalysis(JSON.stringify({ protocol: 2, kind: 'fees', algorithm: 'local-analysis-1', initial_equity: 1000, records: items.map((item, i) => ({ record_id: String(i), trades: [], observation: { id: i, execution_id: null, asset: 999999, sizing_fallback: false, quantity_target: true, ...item, target_weight: item.target } })) }))
  try {
    for (const fee of [0, 0.001, 0.01]) {
      const preview = JSON.parse(kernel.query(JSON.stringify({ fee_rate: fee }))).preview
      expect(preview.sized).toEqual(preview.target)
      expect(preview.sized.used).toBe(2)
      expect(preview.sized.skipped).toBe(1)
      expect(preview.sized.closes['2026-01-01'].cumulative_return).toBe(0)
      expect(preview.sized.closes['2026-01-02'].cumulative_return).toBeCloseTo((1100 - fee * 1100) / 1000 - 1, 12)
    }
  } finally { kernel.free() }
})

test('packaged replay WASM never treats unknown depth as zero', () => {
  const kernel = new ReplayAnalysis(JSON.stringify({ tick: 1, market: [], own: [], depth: [
    { start: 0, end: 10, bids: [{ price: 100, quantity: null }], asks: [{ price: 102, quantity: 1 }], available_depth: 1 },
  ] }))
  try {
    const result = JSON.parse(kernel.query(JSON.stringify({ start: 0, end: 20, levels: 1 })))
    expect(result.spread).toBe(2)
    expect(result.spread_coverage).toBe(0.5)
    expect(result.imbalance).toBeNull()
    expect(result.imbalance_coverage).toBe(0)
  } finally { kernel.free() }
})

test('fractional tick prices keep separate bins in the packaged WASM', () => {
  const kernel = new ReplayAnalysis(JSON.stringify({ tick: 0.01, depth: [], own: [], market: Array.from({ length: 30 }, (_, i) => ({ recording: 'r', key: String(i), time: 1, price: 100.2 + i * 0.01, quantity: 1, stable: true, conflict: false })) }))
  try {
    const result = JSON.parse(kernel.query(JSON.stringify({ start: 0, end: 2, levels: 1 })))
    expect(result.market.profile).toHaveLength(30)
    expect(result.market.profile.every(([, quantity]: [number, number]) => quantity === 1)).toBe(true)
  } finally { kernel.free() }
})

test('selection metadata changes price bins without reloading resident facts', () => {
  const kernel = new ReplayAnalysis(JSON.stringify({ depth: [], own: [], market: [{ recording: 'r', key: 'T', time: 1, price: 100, quantity: 2, stable: true, conflict: false }] }))
  try {
    const query = (tick: number | null) => JSON.parse(kernel.query(JSON.stringify({ start: 0, end: 2, levels: 1, tick })))
    expect(query(1).market).toMatchObject({ vwap: 100, quantity: 2, profile: [[100, 2]], bin_width: 1 })
    expect(query(0.5).market.bin_width).toBe(0.5)
    expect(query(null).market.profile).toEqual([])
  } finally { kernel.free() }
})

test('older mixed input protocol cannot silently become a fee trial', () => {
  expect(() => new AccountAnalysis(JSON.stringify({ protocol: 1, kind: 'fees', algorithm: 'local-analysis-1', initial_equity: 100, records: [] }))).toThrow('版本')
})
