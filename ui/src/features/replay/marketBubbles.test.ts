import { expect, it } from 'bun:test'
import { replayChartLayout } from '@/features/replay/chartGeometry'
import { replayBubbleRadius, replayMarketBubbleGroups, replayMarketBubbles, replayMarketBubblesAt } from '@/features/replay/marketBubbles'
import { replayFutureRecord } from '@/features/replay/inspection'
import type { ReplayCursor, ReplayGap } from '@/features/replay/types'
import type { ReplayTrade } from '@/features/replay/types'

const range = { start_us: 0, end_us: 10_000 }, layout = replayChartLayout(1000, 400)
const trade = (time_us: number, values: Partial<ReplayTrade> = {}): ReplayTrade => ({ time_us, capture_seq: time_us, symbol: 'X', price: 100, quantity: 2, direction: 'BUY', ...values } as ReplayTrade)

it('缩小时同时间像素同价同向聚合，放大还原，保留原始事实与总量', () => {
  const trades = [trade(100), trade(101), trade(102)], before = JSON.stringify(trades)
  const merged = replayMarketBubbles(trades, 'X', range, layout, { time_us: 9999 }, [])
  expect(merged).toHaveLength(1)
  expect(merged[0]).toMatchObject({ time_us: 100, end_us: 102, quantity: 6, trades })
  const zoomed = replayMarketBubbles(trades, 'X', { start_us: 99, end_us: 110 }, layout, { time_us: 9999 }, [])
  expect(zoomed).toHaveLength(3)
  expect(zoomed.reduce((sum, bubble) => sum + bubble.quantity, 0)).toBe(6)
  expect(JSON.stringify(trades)).toBe(before)
  expect(replayBubbleRadius(4, 16) ** 2 / replayBubbleRadius(16, 16) ** 2).toBe(.25)
})

it('真实价格、主动方向、品种、缺口与游标微秒内的序号边界不混合', () => {
  const trades = [trade(100, { capture_seq: 1 }), trade(100, { capture_seq: 2 }), trade(101), trade(102), trade(103, { price: 101 }), trade(104, { direction: 'SELL' }), trade(104, { symbol: 'Y' })]
  const result = replayMarketBubbles(trades, 'X', range, layout, { time_us: 100, capture_seq: 1 }, [{ start_us: 101, end_us: 102, topic: '/market_trade/X' }])
  expect(result).toHaveLength(6)
  expect(result.flatMap(bubble => bubble.trades)).toHaveLength(6)
  expect(result.every(bubble => bubble.trades.length === 1)).toBe(true)
})

it('缺失、零及非法量不画成已知气泡，原始记录不删除', () => {
  const trades = [trade(100, { quantity: null }), trade(100, { quantity: 0 }), trade(100, { quantity: NaN }), trade(100, { price: Infinity }), trade(-1), trade(10_000)]
  expect(replayMarketBubbles(trades, 'X', range, layout, { time_us: 9999 }, [])).toEqual([])
  expect(trades).toHaveLength(6)
})

it('静态分组在游标外复用，只有跨游标组重建，身份质量与原始记录不丢失', () => {
  const trades = [trade(100, { capture_seq: 1, recording_id: 'r', identity_quality: 'stable' }), trade(100, { capture_seq: 2, recording_id: 'r', identity_quality: 'stable', conflict: true }), trade(101, { capture_seq: 3, recording_id: 'r', identity_quality: 'stable' }), trade(1000, { identity_quality: 'stable' })]
  const before = JSON.stringify(trades), groups = replayMarketBubbleGroups(trades, 'X', range, layout, [])
  const all = replayMarketBubblesAt(groups, { time_us: 9999 })
  const split = replayMarketBubblesAt(groups, { time_us: 100, capture_seq: 1 })
  expect(split).toHaveLength(3)
  expect(split[0]).toMatchObject({ quantity: 2, stable: true, trades: [trades[0]] })
  expect(split[1]).toMatchObject({ quantity: 4, stable: false, trades: trades.slice(1, 3) })
  expect(split[2]).toBe(all[1])
  expect(all[0].stable).toBe(false)
  expect(replayMarketBubblesAt(groups, { time_us: 0 })[0]).toBe(all[0])
  expect(replayMarketBubblesAt(groups, { time_us: 9999 })[0]).toBe(all[0])
  expect(JSON.stringify(trades)).toBe(before)
})

it('所有游标分割与原聚合逐项一致，包括乱序输入、重复缺口、浮点量和检查器顺序', () => {
  const trades = [trade(102, { capture_seq: 4, quantity: .1 }), trade(100, { capture_seq: 2, price: 101 }), trade(100, { capture_seq: 1, quantity: .2 }), trade(101, { capture_seq: 3, direction: 'SELL' }), trade(100, { capture_seq: 0, quantity: .3 }), trade(102, { capture_seq: 5, direction: null }), trade(103, { recording_id: 'other', identity_quality: 'stable' })]
  const gaps: ReplayGap[] = [{ start_us: 101, end_us: 102, topic: '/market_trade/X' }, { start_us: 101, end_us: 102 }, { start_us: 0, end_us: 9999, symbol: 'Y' }, { start_us: 100, end_us: 103, topic: 'depth' }]
  // 保留原实现作为等价性基线，避免新旧入口共用代码使测试自证。
  const original = (cursor: ReplayCursor) => {
    const boundaries = [101, 101, 102, 102], groups = new Map<string, { trades: ReplayTrade[]; quantity: number; time_us: number; end_us: number }>()
    for (const trade of trades) {
      const key = JSON.stringify([Math.floor(layout.left + trade.time_us / 10000 * (layout.right - layout.left)), trade.price, trade.direction, boundaries.filter(time => time <= trade.time_us).length, replayFutureRecord(trade.time_us, trade.capture_seq, cursor)])
      const group = groups.get(key)
      if (group) { group.trades.push(trade); group.quantity += trade.quantity!; group.time_us = Math.min(group.time_us, trade.time_us); group.end_us = Math.max(group.end_us, trade.time_us) }
      else groups.set(key, { trades: [trade], quantity: trade.quantity!, time_us: trade.time_us, end_us: trade.time_us })
    }
    return [...groups.values()]
  }
  const groups = replayMarketBubbleGroups(trades, 'X', range, layout, gaps)
  for (const time_us of [99, 100, 101, 102, 103, 104]) for (const capture_seq of [undefined, 0, 1, 2, 3, 4, 5]) {
    const result = replayMarketBubblesAt(groups, { time_us, capture_seq })
    expect(result.map(({ trades, quantity, time_us, end_us }) => ({ trades, quantity, time_us, end_us }))).toEqual(original({ time_us, capture_seq }))
  }
})

it('游标不穿过分组时不再次读取组内成交的量或身份', () => {
  let reads = 0
  const trades = Array.from({ length: 1000 }, (_, index) => Object.defineProperty(trade(100 + index % 2), 'quantity', { get() { reads++; return 2 } }))
  const groups = replayMarketBubbleGroups(trades, 'X', range, layout, [])
  reads = 0
  for (let time_us = 200; time_us < 300; time_us++) replayMarketBubblesAt(groups, { time_us })
  expect(reads).toBe(0)
})
