import { projectReplay } from '@/features/replay/model'
import { expect, it } from 'bun:test'
import { appliedEventIndex, initialReplayCursor, playbackCursor } from '@/features/replay/playback'
import type { ReplayMessage, ReplayWindow } from '@/features/replay/types'

const events: ReplayMessage[] = [1, 3, 7].map(capture_seq => ({ schema_version: 1, recording_id: 'r', capture_seq, time_us: 100, topic: '/order', data: {} }))
it('同微秒事件可停在其中一条之后', () => {
  expect(appliedEventIndex(events, { time_us: 100, capture_seq: 3 })).toBe(1)
  expect(appliedEventIndex(events, { time_us: 99 })).toBe(-1)
})
it('一帧积压受预算限制而不丢事件', () => {
  const first = playbackCursor(events, { time_us: 99 }, 200, 2)
  expect(first).toEqual({ time_us: 100, capture_seq: 3 })
  expect(playbackCursor(events, first, 200, 2)).toEqual({ time_us: 200 })
})

it('默认从有效盘口起播，保留更早业务事件并按采集顺序应用', () => {
  const make = (messages: ReplayMessage[], state: Record<string, unknown> = {}) => ({
    recording_id: 'r', window_id: 'w', context: {}, messages, gaps: [], coverage: [{ start_us: 0, end_us: 1000 }],
    state_before_start: { time_us: 0, state_through_seq: 0, ...state },
  }) as unknown as ReplayWindow
  const message = (seq: number, time: number, topic: string, data: Record<string, unknown>): ReplayMessage => ({ schema_version: 1, recording_id: 'r', capture_seq: seq, time_us: time, topic, data })
  const quote = message(3, 500, '/quote/X', { book_valid: true, bids: [{ price: 10, quantity: 2 }], asks: [{ price: 11, quantity: 2 }] })
  const invalid = message(1, 100, '/quote/X', { book_valid: false })
  const other = message(2, 200, '/quote/Y', { book_valid: true })
  const range = { start_us: 0, end_us: 1000 }
  const start = (window: ReplayWindow) => initialReplayCursor(window, projectReplay(window, { time_us: 999 }), 'X', range)
  expect(start(make([invalid, other, quote]))).toEqual({ time_us: 500, capture_seq: 3 })
  for (const topic of ['/order', '/trade', '/command']) {
    const business = message(2, 200, topic, { symbol: 'X', order_id: 'A', price: 10, quantity: 1, command_id: 'C', operation: 'insert', phase: 'request' })
    expect(start(make([invalid, business, quote]))).toEqual({ time_us: 200, capture_seq: 2 })
  }
  const window = make([quote])
  const cursor = start(window)
  expect(projectReplay(window, cursor).quotes.X.book_valid).toBe(true)
  expect(start(make([invalid]))).toEqual({ time_us: 0, capture_seq: 0 })
  expect(start(make([quote], { quotes: { X: { symbol: 'X', book_valid: true, available: true, time_us: 0, capture_seq: 0, valid_until_us: 1000 } } }))).toEqual({ time_us: 0, capture_seq: 0 })
})
