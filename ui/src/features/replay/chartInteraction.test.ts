import { describe, expect, it } from 'bun:test'
import { replayContentRange, clampReplayView, replayOrderTimeline, replayVisibleOrders, zoomReplayPriceAt, zoomReplayTime } from '@/features/replay/chartInteraction'
import type { ReplayOrder, ReplayProjection } from '@/features/replay/types'

const bounds = { start_us: 0, end_us: 10_000_000 }
const order = (time_us: number, values: Partial<ReplayOrder> = {}): ReplayOrder => ({
  key: 'O', order_id: 'O', symbol: 'X', price: 100, volume: 2, remaining_volume: 2, filled_volume: 0,
  direction: 'BUY', offset: null, status: 'working', confirmed: true, cancel_pending: false, terminal: false,
  carry_in: false, execution_id: 'E', previous_order_id: null, time_us, capture_seq: time_us, attributes: {}, native_identity: {}, ...values,
})
const timeline = (rows: ReplayOrder[], time_us = 999, capture_seq?: number) => replayOrderTimeline(rows, 'X', { start_us: 100, end_us: 1000 }, { time_us, capture_seq })

describe('图表浏览与订单生命周期', () => {
  it('时间缩放保持鼠标锚点比例，并夹在已读取窗口内', () => {
    const view = zoomReplayTime(bounds, .5, 2_000_000, bounds)
    expect(view).toEqual({ start_us: 1_000_000, end_us: 6_000_000 })
    expect((2_000_000 - view.start_us) / (view.end_us - view.start_us)).toBe(.2)
    expect(clampReplayView({ start_us: -2_000_000, end_us: 3_000_000 }, bounds)).toEqual({ start_us: 0, end_us: 5_000_000 })
    expect(clampReplayView({ start_us: 9_000_000, end_us: 14_000_000 }, bounds)).toEqual({ start_us: 5_000_000, end_us: 10_000_000 })
    expect(zoomReplayTime(bounds, 4, 2_000_000, bounds)).toEqual(bounds)
  })
  it('价格缩放保留指向的真实价位，最小范围为四个价格跳动', () => {
    const view = { min: 90, max: 110, tick: 1 }
    expect(zoomReplayPriceAt(view, .5, 95)).toEqual({ min: 92.5, max: 102.5, tick: 1 })
    expect(zoomReplayPriceAt(view, .01, 95)).toEqual({ min: 94, max: 98, tick: 1 })
  })
  it('观察到挂单、改价与成交时，轨迹在终态停止', () => {
    const result = timeline([order(150), order(250, { price: 101 }), order(400, { price: 101, status: 'filled', terminal: true, remaining_volume: 0 })])
    expect(result.marks.map(mark => [mark.kind, mark.time, mark.price])).toEqual([['placed', 150, 100], ['changed', 250, 101], ['filled', 400, 101]])
    expect(result.segments.map(segment => [segment.start, segment.end])).toEqual([[150, 250], [250, 400]])
  })
  it('首个观察就是成交时只画终点，不补造挂单线与起点', () => {
    const result = timeline([order(300, { terminal: true, status: 'filled', remaining_volume: 0 })])
    expect(result.segments).toEqual([])
    expect(result.marks.map(mark => mark.kind)).toEqual(['filled'])
  })
  it('部分成交与待撤保持有效线，确认撤销才终止，改价只连接确认后的价位', () => {
    const rows = [order(150), order(200, { status: 'partial', filled_volume: 1, remaining_volume: 1 }), order(250, { cancel_pending: true }), order(300, { terminal: true, status: 'canceled' })]
    const result = timeline(rows)
    expect(result.marks.map(mark => mark.kind)).toEqual(['placed', 'canceled'])
    expect(result.segments.map(segment => [segment.start, segment.end])).toEqual([[150, 200], [200, 250], [250, 300]])
    expect(result.segments[1].order.remaining_volume).toBe(1)
    expect(timeline([order(150), order(200, { confirmed: false, price: 101 })]).marks.map(mark => mark.kind)).toEqual(['placed'])
  })
  it('图层隐藏完整终态轨迹，保留原始事实与其他委托，不改变当前盘口状态', () => {
    const rows = [order(150), order(250, { terminal: true, status: 'filled' }), order(300, { key: 'B', order_id: 'B' })], before = JSON.stringify(rows)
    expect(replayVisibleOrders(rows, { working: true, canceled: true, filled: false })).toEqual([rows[2]])
    expect(replayVisibleOrders(rows, { working: false, canceled: true, filled: true })).toEqual(rows.slice(0, 2))
    expect(JSON.stringify(rows)).toBe(before)
  })
  it.each(['canceled', 'expired', 'rejected'])('%s 使用真实终态标记且不延长', status => {
    const result = timeline([order(150), order(250, { terminal: true, status })])
    expect(result.marks.map(mark => mark.kind)).toEqual(['placed', status])
    expect(result.segments[0].end).toBe(250)
  })
  it('窗口前已存在的单没有新挂单标记，未知状态不冒充撤单', () => {
    const result = timeline([order(50, { carry_in: true }), order(250, { status: 'unknown', confirmed: false })])
    expect(result.marks).toEqual([])
    expect(result.segments.map(segment => [segment.start, segment.end, segment.entry])).toEqual([[100, 250, false]])
  })
  it('终态重复观察不产生重复端点，未来、他品种和同微秒后序观察不进入轨迹', () => {
    const rows = [order(200, { capture_seq: 3 }), order(200, { capture_seq: 4, terminal: true, status: 'filled' }), order(200, { capture_seq: 5, terminal: true, status: 'filled' }), order(400), order(150, { symbol: 'Y' })]
    expect(timeline(rows, 200, 3).marks.map(mark => mark.kind)).toEqual(['placed'])
    expect(timeline(rows, 300, 5).marks.map(mark => mark.kind)).toEqual(['placed', 'filled'])
  })
})


describe('默认时间视野', () => {
  const scene = { quoteHistory: [], orderHistory: [], marketTrades: [], ownTrades: [] } as unknown as ReplayProjection
  it('跳过空白前缀，保留更早委托和窗口前延续状态', () => {
    const withOrders = (times: number[]) => ({ ...scene, orderHistory: times.map(time => order(time)) })
    expect(replayContentRange(withOrders([3_000_000]), 'X', bounds)).toEqual({ ...bounds, start_us: 3_000_000 })
    expect(replayContentRange(withOrders([-100]), 'X', bounds)).toEqual(bounds)
    expect(replayContentRange(withOrders([3_000_000]), 'Y', bounds)).toEqual(bounds)
    expect(replayContentRange(scene, 'X', bounds)).toEqual(bounds)
  })
})
