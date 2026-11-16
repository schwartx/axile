import { currentReplayQuote } from '@/features/replay/chartGeometry'
import type { ReplayCursor, ReplayMessage, ReplayProjection, ReplayRange, ReplayWindow } from '@/features/replay/types'

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

/** 只跳过开头无内容的时段，保留更早的业务事件与窗口前状态。 */
export function initialReplayCursor(window: ReplayWindow, scene: ReplayProjection, symbol: string, range: ReplayRange): ReplayCursor {
  const fallback = { time_us: range.start_us, capture_seq: Number(window.state_before_start?.state_through_seq ?? 0) }
  const records = [...scene.quoteHistory.filter(quote => currentReplayQuote(quote, Math.max(range.start_us, quote.time_us), window.gaps)), ...scene.orderHistory, ...scene.ownTrades, ...scene.marketTrades, ...scene.commands]
  const sequences = new Set<number>()
  for (const row of records) {
    if (row.symbol !== symbol) continue
    if (row.capture_seq <= fallback.capture_seq) return fallback
    sequences.add(row.capture_seq)
  }
  const first = window.messages.find(row => row.time_us >= range.start_us && row.time_us < range.end_us && sequences.has(row.capture_seq))
  return first ? { time_us: first.time_us, capture_seq: first.capture_seq } : fallback
}
