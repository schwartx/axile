import { describe, expect, it } from 'bun:test'
import { centerReplayPrice, currentReplayQuote, defaultReplayPriceView, replayChartLayout, replayHeatCells, replayMarketPriceSegments, replayOnBookOrder, replayPriceTicks, replayPriceY, replayQuoteEnd, replayRemainingQuantity, replayXTime, replayYPrice, zoomReplayPrice } from '@/features/replay/chartGeometry'
import type { ReplayOrder, ReplayQuote, ReplayTrade } from '@/features/replay/types'

const quote = (time: number, quantity = 5): ReplayQuote => ({ symbol: 'X', bids: [{ price: 100, quantity }], asks: [{ price: 101, quantity: 4 }], book_valid: true, valid_until_us: 1000, available_depth: 1, depth_coverage: null, last_price: null, last_price_kind: 'unknown', time_us: time, capture_seq: time })
const view = { min: 96, max: 105, tick: 1 }
const range = { start_us: 0, end_us: 1000 }

describe('回放共用价格与观测边界', () => {
  it('主图、盘口和委托同价共享一份投影，缩放后仍可反解', () => {
    const layout = replayChartLayout(1100, 440)
    const zoomed = zoomReplayPrice(view, 0.7)
    expect(replayYPrice(replayPriceY(100, zoomed, layout), zoomed, layout)).toBeCloseTo(100)
    expect(replayChartLayout(390, 380).columns).toBe(false)
    expect(replayXTime(layout.right, range, layout)).toBe(999)
  })
  it('盘口过期与缺口不被旧深度延续，未知有效期只保留观测点', () => {
    const gap = [{ start_us: 500, end_us: 700, symbol: 'X' }]
    expect(replayQuoteEnd(quote(100), 900, 800, gap)).toBe(500)
    expect(currentReplayQuote(quote(100), 600, gap)).toBeNull()
    expect(currentReplayQuote(quote(100), 800, gap)).toBeNull()
    expect(currentReplayQuote(quote(750), 800, gap)).not.toBeNull()
    expect(replayQuoteEnd({ ...quote(100), valid_until_us: null }, 900, 800, [])).toBe(100)
  })
  it('同一微秒的大挂量保留峰值，未来盘口不进入热力图', () => {
    const layout = replayChartLayout(1000, 400)
    const result = replayHeatCells([quote(100, 2), quote(100, 500), quote(110, 3), quote(900, 9999)], range, 300, view, layout, [])
    expect(Math.max(...result.cells.map(cell => cell.quantity ?? 0))).toBe(500)
    expect(result.cells.some(cell => cell.quantity === 9999)).toBe(false)
    expect(result.cells.filter(cell => cell.quantity === 500).every(cell => cell.y === replayPriceY(100, view, layout))).toBe(true)
  })
  it('初始价位范围只依赖已观察价位，不伪造缺失档位', () => {
    expect(defaultReplayPriceView(undefined, [], null)).toBeNull()
    expect(defaultReplayPriceView(quote(100), [], null)).toEqual({ min: 98, max: 103, tick: 1 })
    const marketTrade = { price: 99 } as ReplayTrade
    expect(defaultReplayPriceView(undefined, [], 1, [marketTrade])).toEqual({ min: 97, max: 101, tick: 1 })
    expect(defaultReplayPriceView(quote(100), [], 1, [{ price: 500 } as ReplayTrade])).toEqual({ min: 98, max: 103, tick: 1 })
  })
  it('委托聚焦只平移共用价格轴，保持真实价差与缩放比例', () => {
    const focused = centerReplayPrice(view, 90)
    const layout = replayChartLayout(640, 400)
    expect(focused).toEqual({ min: 85.5, max: 94.5, tick: 1 })
    expect(replayPriceY(90, focused, layout)).toBe((layout.top + layout.bottom) / 2)
    expect(layout.columns).toBe(true)
    expect(layout.priceX).toBeLessThan(layout.left)
    expect(layout.cobX).toBeGreaterThan(layout.right)
  })
  it('场内单剩余量未知时保留身份，同价聚合不把未知量当零', () => {
    const order = { confirmed: true, terminal: false, price: 100, remaining_volume: null } as ReplayOrder
    expect(replayOnBookOrder(order)).toBe(true)
    expect(replayOnBookOrder({ ...order, remaining_volume: 0 })).toBe(false)
    expect(replayRemainingQuantity([{ remaining_volume: 3 }, { remaining_volume: null }])).toBeNull()
    expect(replayRemainingQuantity([{ remaining_volume: 3 }, { remaining_volume: 2 }])).toBe(5)
  })
  it('市场价格线不读取未来、其他品种、无效价或窗口外记录', () => {
    const trade = (time_us: number, price = 100): ReplayTrade => ({ symbol: 'X', time_us, capture_seq: time_us, price, conflict: false } as ReplayTrade)
    const trades = [trade(50), trade(150), trade(250), trade(350), trade(1000), trade(200, NaN), { ...trade(220), symbol: 'Y' }, { ...trade(230), conflict: true }]
    const segments = replayMarketPriceSegments(trades, 'X', { start_us: 100, end_us: 1000 }, 300, [])
    expect(segments.map(segment => segment.map(point => point.time_us))).toEqual([[150], [250]])
    expect(replayMarketPriceSegments([], 'X', range, 300, [])).toEqual([])
  })
  it.each(['unknown', 'conflict'])('市场价格线在%s观察处断开，不把两边有效价补成连续线', kind => {
    const trades = [100, 200, 300].map(time_us => ({ symbol: 'X', time_us, capture_seq: time_us, price: 100, conflict: false } as ReplayTrade))
    if (kind === 'unknown') trades[1].price = null
    else trades[1].conflict = true
    expect(replayMarketPriceSegments(trades, 'X', range, 400, []).map(segment => segment.map(point => point.time_us))).toEqual([[100], [300]])
  })
  it('市场价格线在相关缺口两边分段，缺口内点不参与连接', () => {
    const trades = [100, 200, 300, 400, 500, 600].map(time_us => ({ symbol: 'X', time_us, capture_seq: time_us, price: 100, conflict: false } as ReplayTrade))
    const segments = replayMarketPriceSegments(trades, 'X', range, 700, [{ start_us: 250, end_us: 400, topic: '/market_trade/X', symbol: 'X' }])
    expect(segments.map(segment => segment.map(point => point.time_us))).toEqual([[100, 200], [400, 500, 600]])
    const betweenPoints = replayMarketPriceSegments(trades, 'X', range, 700, [{ start_us: 225, end_us: 250 }])
    expect(betweenPoints.map(segment => segment.map(point => point.time_us))).toEqual([[100, 200], [300, 400, 500, 600]])
    const unrelated = replayMarketPriceSegments(trades, 'X', range, 700, [{ start_us: 250, end_us: 400, topic: 'depth' }, { start_us: 250, end_us: 400, symbol: 'Y' }])
    expect(unrelated).toHaveLength(1)
  })
})

it('价格轴容纳完整读数，刻度规整且不累加推算价差的浮点尾数', () => {
  const layout = replayChartLayout(1100, 440, 124)
  expect(layout.left).toBe(124)
  expect(layout.priceX).toBe(116)
  expect(layout.top).toBe(0)
  expect(replayPriceTicks({ min: 90100, max: 90140, tick: 0.09999999999126885 }, 240)).toEqual([90100, 90105, 90110, 90115, 90120, 90125, 90130, 90135, 90140])
  expect(replayPriceTicks({ min: 0.00000001, max: 0.00000005, tick: 0.00000001 }, 240)).toEqual([0.00000001, 0.00000002, 0.00000003, 0.00000004, 0.00000005])
  expect(replayPriceTicks({ min: 100, max: 101, tick: 0.25 }, 240)).toEqual([100, 100.5, 101])
})
