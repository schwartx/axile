import { expect, it } from 'bun:test'
import { replayChartLayout } from '@/features/replay/chartGeometry'
import { replayBubbleRadius, replayMarketBubbles } from '@/features/replay/marketBubbles'
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
