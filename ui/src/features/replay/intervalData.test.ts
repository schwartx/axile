import { expect, test } from 'bun:test'
import { intervalInputs, intervalMetadata } from './intervalData'
import { projectReplay } from './model'
import { replayExecutionScene } from './executionScene'
import type { ReplayExecutionSlice } from './executionScene'
import type { ReplayQuote, ReplayWindow } from './types'

test('raw intervals stop at gaps and recording ends; unknown depth stays unknown', () => {
  const quote: ReplayQuote = { symbol: 'A', time_us: 10, capture_seq: 1, bids: [{ price: 100, quantity: null }], asks: [{ price: 101, quantity: 3 }], book_valid: true, available: true, available_depth: 1, valid_until_us: 100, depth_coverage: null, last_price: null, last_price_kind: 'unknown' }
  const window = { recording_id: 'r1', window_id: 'w', state_before_start: null, messages: [], next_cursor: null, complete: true, coverage: [], gaps: [], context: { schema_version: 1 } }
  const scene = { ...projectReplay(window, { time_us: 80 }), quoteHistory: [quote] }
  const range = { start_us: 0, end_us: 80 }
  const slice: ReplayExecutionSlice = { range, scene, tradeFacts: scene, project: () => scene, window: { recording_id: 'r1', window_id: 'w', state_before_start: null, messages: [], next_cursor: null, complete: true, coverage: [range], gaps: [{ start_us: 40, end_us: 60, topic: 'depth' }], context: { schema_version: 1, instrument: { A: { price_tick: 1 } }, streams: { A: { market_trade: { availability: 'unavailable', source: 'ctp' } } } } } }
  const packed = intervalInputs([slice], 'A')
  expect(packed.depth).toEqual([{ start: 10, end: 40, bids: quote.bids, asks: quote.asks, available_depth: 1 }])
  expect(packed.depth[0].bids[0].quantity).toBeNull()
  expect(intervalMetadata([slice], 'A', range).availability).toBe('unavailable')
  expect(intervalMetadata([slice], 'A', range).tick).toBe(1)
  slice.window.context.streams!.A.market_trade.availability = 'available'
  slice.window.gaps = [{ start_us: 30, end_us: 40, topic: '/market_trade/A', symbol: 'A' }]
  expect(intervalMetadata([slice], 'A', range).availability).toBe('partial')
})


function splitTrades(stream: 'market' | 'own', initial: { price: number; quantity: number | null }, update: { price: number; quantity: number }) {
  const context = { schema_version: 1, instrument: { X: { price_tick: 1 } }, streams: { X: { market_trade: { availability: 'available' as const, source: 'test' } } } }
  const fact = { symbol: 'X', trade_key: 'T', identity_quality: 'stable', ...initial }
  const topic = stream === 'market' ? '/market_trade/X' : '/trade'
  const first: ReplayWindow = { recording_id: 'r', window_id: 'a', context, coverage: [{ start_us: 0, end_us: 100 }], gaps: [], complete: true, next_cursor: null, state_before_start: null,
    messages: [{ schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 50, topic, data: fact }] }
  const second: ReplayWindow = { ...first, window_id: 'b', coverage: [{ start_us: 100, end_us: 200 }],
    state_before_start: { time_us: 100, state_through_seq: 1, trade_accounting: { [`${stream}_seen_keys`]: ['T'], [`${stream}_records`]: { T: { data: fact, first_time_us: 50, first_capture_seq: 1, conflict: false } } } },
    messages: [{ schema_version: 1, recording_id: 'r', capture_seq: 2, time_us: 150, topic, data: { ...fact, ...update } }] }
  return replayExecutionScene([{ window: first, range: first.coverage[0] }, { window: second, range: second.coverage[0] }], { start_us: 0, end_us: 200 }, [])!
}

for (const stream of ['market', 'own'] as const) {
  test(`${stream} late conflict crosses split windows without becoming a valid fill`, () => {
    const overview = splitTrades(stream, { price: 100, quantity: 1 }, { price: 101, quantity: 1 })
    const field = stream === 'market' ? 'marketTrades' : 'ownTrades'
    expect(overview.slices[1].scene[field]).toHaveLength(0)
    expect(overview.slices[1].tradeFacts[field][0].conflict).toBe(true)
    const facts = intervalInputs(overview.slices, 'X')[stream]
    expect(facts).toHaveLength(1)
    expect(facts[0]).toMatchObject({ recording: 'r', key: 'T', time: 50, conflict: true })
  })
  test(`${stream} late quantity completion replaces the incomplete first-window fact`, () => {
    const overview = splitTrades(stream, { price: 100, quantity: null }, { price: 100, quantity: 2 })
    expect(intervalInputs(overview.slices, 'X')[stream]).toEqual([{ recording: 'r', key: 'T', time: 50, price: 100, quantity: 2, stable: true, conflict: false }])
  })
}

test('metadata and tick follow only the selected half-open range', () => {
  const overview = splitTrades('market', { price: 100, quantity: 1 }, { price: 100, quantity: 1 })
  overview.slices[1].window = { ...overview.slices[1].window, context: { schema_version: 1, streams: { X: { market_trade: { availability: 'unavailable' } } } } }
  const first = intervalMetadata(overview.slices, 'X', { start_us: 0, end_us: 100 })
  expect(first).toEqual({ tick: 1, availability: 'available', sources: ['test'] })
  expect(intervalMetadata(overview.slices, 'X', { start_us: 100, end_us: 200 }).availability).toBe('unavailable')
  expect(intervalMetadata(overview.slices, 'X', { start_us: 0, end_us: 200 })).toMatchObject({ tick: null, availability: 'partial' })
  overview.slices[0].window.gaps = [{ start_us: 70, end_us: 90, topic: '/market_trade/X' }]
  expect(intervalMetadata(overview.slices, 'X', { start_us: 0, end_us: 60 }).availability).toBe('available')
  expect(intervalMetadata(overview.slices, 'X', { start_us: 0, end_us: 100 }).availability).toBe('partial')
})

test('recording identity is preserved and market checkpoints may omit old trades', () => {
  const overview = splitTrades('market', { price: 100, quantity: 1 }, { price: 100, quantity: 1 })
  overview.slices[1].tradeFacts = { ...overview.slices[1].tradeFacts, marketTrades: [] }
  expect(intervalInputs(overview.slices, 'X').market).toHaveLength(1)
  const other = { ...overview.slices[0], window: { ...overview.slices[0].window, recording_id: 'other' } }
  expect(intervalInputs([...overview.slices, other], 'X').market.map(t => t.recording).sort()).toEqual(['other', 'r'])
})

test('a later stable association removes the superseded weak observation', () => {
  const context = { schema_version: 1 }
  const weak = { symbol: 'X', price: 100, quantity: 2, identity_quality: 'unknown' }
  const first: ReplayWindow = { recording_id: 'r', window_id: 'a', context, coverage: [{ start_us: 0, end_us: 100 }], gaps: [], complete: true, next_cursor: null, state_before_start: null,
    messages: [{ schema_version: 1, recording_id: 'r', capture_seq: 1, time_us: 50, topic: '/trade', data: weak }] }
  const second: ReplayWindow = { ...first, window_id: 'b', coverage: [{ start_us: 100, end_us: 200 }],
    state_before_start: { time_us: 100, state_through_seq: 1, trade_accounting: { pending: { 'own:observation:1': { market: false, observation: { data: weak, first_time_us: 50, first_capture_seq: 1 } } } } },
    messages: [{ schema_version: 1, recording_id: 'r', capture_seq: 2, time_us: 150, topic: '/trade', data: { ...weak, identity_quality: 'stable', trade_key: 'T', related_capture_seq: 1 } }] }
  const overview = replayExecutionScene([{ window: first, range: first.coverage[0] }, { window: second, range: second.coverage[0] }], { start_us: 0, end_us: 200 }, [])!
  expect(intervalInputs(overview.slices, 'X').own).toEqual([{ recording: 'r', key: 'T', time: 150, price: 100, quantity: 2, stable: true, conflict: false }])
})
