import { describe, expect, it } from 'bun:test'
import { currentReplayQuote, defaultReplayPriceView, replayChartLayout, replayHeatCells, replayOnBookOrder, replayPriceY, replayQuoteEnd, replayRemainingQuantity, replayXTime, replayYPrice, zoomReplayPrice } from '@/features/replay/chartGeometry'
import type { ReplayOrder, ReplayQuote } from '@/features/replay/types'

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
    expect(replayQuoteEnd({ ...quote(100), valid_until_us: null }, 900, 800, [])).toBe(100)
  })
  it('同一微秒的大挂量保留峰值，未来盘口不进入热力图', () => {
    const layout = replayChartLayout(1000, 400)
    const result = replayHeatCells([quote(100, 2), quote(100, 500), quote(110, 3), quote(900, 9999)], range, 300, view, layout, [])
    expect(Math.max(...result.cells.map(cell => cell.quantity))).toBe(500)
    expect(result.cells.some(cell => cell.quantity === 9999)).toBe(false)
    expect(result.cells.filter(cell => cell.quantity === 500).every(cell => cell.y === replayPriceY(100, view, layout))).toBe(true)
  })
  it('初始价位范围只依赖已观察价位，不伪造缺失档位', () => {
    expect(defaultReplayPriceView(undefined, [], null)).toBeNull()
    expect(defaultReplayPriceView(quote(100), [], null)).toEqual({ min: 98, max: 103, tick: 1 })
  })
  it('场内单剩余量未知时保留身份，同价聚合不把未知量当零', () => {
    const order = { confirmed: true, terminal: false, price: 100, remaining_volume: null } as ReplayOrder
    expect(replayOnBookOrder(order)).toBe(true)
    expect(replayOnBookOrder({ ...order, remaining_volume: 0 })).toBe(false)
    expect(replayRemainingQuantity([{ remaining_volume: 3 }, { remaining_volume: null }])).toBeNull()
    expect(replayRemainingQuantity([{ remaining_volume: 3 }, { remaining_volume: 2 }])).toBe(5)
  })
})
