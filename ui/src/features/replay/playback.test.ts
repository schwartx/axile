import { expect, it } from 'bun:test'
import { appliedEventIndex, playbackCursor } from '@/features/replay/playback'
import type { ReplayMessage } from '@/features/replay/types'

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
