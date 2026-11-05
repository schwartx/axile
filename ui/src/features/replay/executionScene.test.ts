import { expect, it } from 'bun:test'
import { replayExecutionAt, replayExecutionScene } from '@/features/replay/executionScene'
import { replayTradeKey } from '@/features/replay/chartInteraction'
import type { ReplayWindow } from '@/features/replay/types'

const windowAt = (recording_id: string, start: number): ReplayWindow => ({
  recording_id, window_id: recording_id, context: { schema_version: 1 }, complete: true, next_cursor: null,
  coverage: [{ start_us: start, end_us: start + 10 }], gaps: [],
  state_before_start: { recording_id, time_us: start, state_through_seq: 0 },
  messages: [
    { schema_version: 1, recording_id, capture_seq: 1, time_us: start + 1, topic: '/quote/X', data: { symbol: 'X', book_valid: true, valid_until_us: start + 50, bids: [{ price: 100, quantity: start + 1 }] } },
    { schema_version: 1, recording_id, capture_seq: 2, time_us: start + 2, topic: '/trade', data: { symbol: 'X', trade_key: 'same', identity_quality: 'stable', quantity: 1, price: 100 } },
  ],
})

it('各段分别恢复并截断盘口，在缺口不沿用状态，重复序号仍保留各自身份', () => {
  const parts = [0, 20].map((start, index) => ({ range: { start_us: start, end_us: start + 10 }, window: windowAt(`r${index}`, start) }))
  const overview = replayExecutionScene(parts, { start_us: 0, end_us: 30 }, [{ start_us: 10, end_us: 20 }])!
  expect(overview.scene.quoteHistory.map(quote => [quote.time_us, quote.valid_until_us])).toEqual([[1, 10], [21, 30]])
  expect(overview.scene.ownTrades.map(replayTradeKey)).toEqual(['r0:2:same', 'r1:2:same'])
  expect(replayExecutionAt(overview, { time_us: 5 }, 'X').quotes.X.bids[0].quantity).toBe(1)
  expect(replayExecutionAt(overview, { time_us: 15 }, 'X')).toMatchObject({ quotes: {}, orders: {}, warnings: ['当前时刻未录制'] })
  const later = replayExecutionAt(overview, { time_us: 25 }, 'X')
  expect(later.quotes.X.bids[0].quantity).toBe(21)
  expect(later.ownTrades.map(replayTradeKey)).toEqual(['r0:2:same', 'r1:2:same'])
  expect(replayExecutionAt(overview, { time_us: 5 }, 'X').ownTrades.map(replayTradeKey)).toEqual(['r0:2:same'])
})

it('纯盘口定位复用成交标签和到达列表，补全及同微秒边界仍按游标恢复', () => {
  const window = windowAt('r', 0)
  window.messages[1].data = { ...window.messages[1].data, quantity: null }
  window.messages.push(
    { schema_version: 1, recording_id: 'r', capture_seq: 3, time_us: 3, topic: '/quote/X', data: { symbol: 'X', bids: [{ price: 100, quantity: 2 }] } },
    { schema_version: 1, recording_id: 'r', capture_seq: 4, time_us: 4, topic: '/trade', data: { symbol: 'X', trade_key: 'same', identity_quality: 'stable', quantity: 2, price: 100 } },
    { schema_version: 1, recording_id: 'r', capture_seq: 5, time_us: 4, topic: '/trade', data: { symbol: 'X', trade_key: 'second', identity_quality: 'stable', quantity: 1, price: 100 } },
  )
  const overview = replayExecutionScene([{ range: window.coverage[0], window }], window.coverage[0], [])!
  const early = replayExecutionAt(overview, { time_us: 2 }, 'X')
  const quote = replayExecutionAt(overview, { time_us: 3 }, 'X')
  expect(quote.ownTrades).toBe(early.ownTrades)
  expect(quote.marketTrades).toBe(early.marketTrades)
  expect(quote.orderHistory).toBe(early.orderHistory)
  expect(early.ownTrades[0].quantity).toBeNull()
  const completed = replayExecutionAt(overview, { time_us: 4, capture_seq: 4 }, 'X')
  expect(completed.ownTrades.map(trade => [trade.key, trade.quantity])).toEqual([['same', 2]])
  const all = replayExecutionAt(overview, { time_us: 4 }, 'X')
  expect(all.ownTrades.map(trade => trade.key)).toEqual(['same', 'second'])
  expect(replayExecutionAt(overview, { time_us: 4, capture_seq: 4 }, 'X').ownTrades).toEqual(completed.ownTrades)
  expect(replayExecutionAt(overview, { time_us: 3 }, 'X').ownTrades).toEqual(early.ownTrades)
  expect(early.ownTrades[0].quantity).toBeNull()
})

it('录制重叠时不猜测即时盘口属于哪一段', () => {
  const first = windowAt('a', 0), second = windowAt('b', 5)
  const overview = replayExecutionScene([{ range: first.coverage[0], window: first }, { range: second.coverage[0], window: second }], { start_us: 0, end_us: 15 }, [])!
  expect(replayExecutionAt(overview, { time_us: 7 }, 'X')).toMatchObject({ quotes: {}, orders: {}, warnings: ['录制时段重叠，当前状态无法确定'] })
})
