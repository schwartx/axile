import type { ReplayCursor, ReplayMessage } from '@/features/replay/types'

export function appliedEventIndex(messages: ReplayMessage[], cursor: ReplayCursor): number {
  let low = 0, high = messages.length
  while (low < high) {
    const middle = (low + high) >>> 1, row = messages[middle]
    if (row.time_us < cursor.time_us || (row.time_us === cursor.time_us && (cursor.capture_seq == null || row.capture_seq <= cursor.capture_seq))) low = middle + 1
    else high = middle
  }
  return low - 1
}

/** 同微秒事件逐条停留，帧预算不足时保留剩余事件，而非跳过。 */
export function playbackCursor(messages: ReplayMessage[], current: ReplayCursor, targetUs: number, maxEvents = 1000): ReplayCursor {
  const first = appliedEventIndex(messages, current) + 1
  const target = appliedEventIndex(messages, { time_us: targetUs })
  if (target - first + 1 > maxEvents) {
    const event = messages[first + maxEvents - 1]
    return { time_us: event.time_us, capture_seq: event.capture_seq }
  }
  return { time_us: targetUs }
}
