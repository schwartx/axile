import { expect, test } from 'bun:test'
import { displayMessages } from './rawDisplay'
import { projectReplay } from './model'

const observation = { schema_version: 2, recording_id: 'R', capture_seq: 1, time_us: 10, topic: '/quote/S', data: { symbol: 'S', book_valid: true, valid_until_us: 100, bids: [[0.003876, 250]], asks: [[0.003877, 100]] } }
test('V2 display keeps full events and tuple depth for the original projection', () => {
  const messages = displayMessages([observation], 'R')
  expect(messages[0]).toEqual(observation)
  const projection = projectReplay({ recording_id: 'R', window_id: 'c', messages, state_before_start: { schema_version: 2, recording_id: 'R', state_through_seq: 0, time_us: 10 }, coverage: [{ start_us: 10, end_us: 11 }], gaps: [], context: { schema_version: 2 }, next_cursor: null, complete: true }, { time_us: 10 })
  expect(projection.quotes.S.bids).toEqual([{ price: 0.003876, quantity: 250 }])
})
test('V2 display rejects identity mismatches, duplicate observations and time rollback', () => {
  for (const invalid of [{ recording_id: 'other' }, { schema_version: 1 }, { capture_seq: 0 }]) {
    expect(() => displayMessages([{ ...observation, ...invalid }], 'R')).toThrow('REPLAY_DISPLAY_EVENT')
  }
  expect(() => displayMessages([observation, observation], 'R')).toThrow('REPLAY_DISPLAY_EVENT')
  expect(displayMessages([{ ...observation, capture_seq: 3 }], 'R')[0].capture_seq).toBe(3)
  expect(() => displayMessages([observation, { ...observation, capture_seq: 2, time_us: 9 }], 'R')).toThrow('REPLAY_DISPLAY_EVENT')
})
