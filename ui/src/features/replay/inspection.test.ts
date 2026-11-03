import { expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ReplayInspector } from '@/features/replay/ReplayInspector'
import { inspectReplayOrder, inspectReplayTrade, nearestReplayInspection, replayInspectionObjects, type InspectionRecord } from '@/features/replay/inspection'
import { replayVisibleOrderMarks, type OrderMark } from '@/features/replay/chartInteraction'
import { replayChartLayout } from '@/features/replay/chartGeometry'
import type { ReplayOrder, ReplayTrade } from '@/features/replay/types'

const order = { key: 'O', order_id: 'O', symbol: 'X', price: 100, volume: 2, remaining_volume: 1, direction: 'SELL', status: 'working', capture_seq: 2, time_us: 50, execution_id: 'E' } as ReplayOrder
const format = { executionId: 'E', cursor: { time_us: 50, capture_seq: 1 }, unit: '手', price: String, quantity: (value: number | null) => value == null ? '未知' : String(value) }
const record = (id: string, x: number) => ({ id, object: id, x, y: 10 } as InspectionRecord)

it('单条市场记录显示真实记录详情而非聚合说明', () => {
  const trade = { symbol: 'X', price: 100, quantity: 1, direction: 'SELL', attributes: {}, granularity: 'aggregate', capture_seq: 3, time_us: 50, identity_quality: 'stable', source: 'feed' } as ReplayTrade
  const item = inspectReplayTrade(trade, true, 'group', format, 11, 20)
  const summary = { ...item, id: 'group', raw: '像素聚合说明' }
  const html = renderToStaticMarkup(createElement(ReplayInspector, { record: summary, records: [summary, item], onChoose: () => {}, onView: () => {}, onSeek: () => {}, onClose: () => {} }))
  expect(html).toContain('记录详情')
  expect(html).toContain('feed')
  expect(html).toContain('源聚合成交，不拆逐笔')
  expect(html).not.toContain('像素聚合说明')
})

it('摘要按对象提供状态量价，原始身份单独保留，未来按同微秒seq判断', () => {
  const result = inspectReplayOrder(order, format, 10, 20)
  expect(result).toMatchObject({ kind: 'order', headline: '卖 2 手 @ 100', status: '已受理 · 剩余 1 手', future: true, raw: '委托 O · #2', x: 10, y: 20 })
  expect(inspectReplayOrder({ ...order, remaining_volume: null }, format, 10, 20).status).toContain('未知')
  const trade = { order_id: 'O', symbol: 'X', price: 100, quantity: 1, direction: 'SELL', attributes: {}, key: 'F', granularity: 'aggregate', capture_seq: 3, time_us: 50, identity_quality: 'stable' } as ReplayTrade
  expect(inspectReplayTrade(trade, true, 'group', format, 11, 20)).toMatchObject({ kind: 'market', status: '源聚合成交，不拆逐笔', object: 'group', trade: undefined })
  expect(inspectReplayTrade(trade, false, 'order:O', format, 11, 20).trade).toBe('3:F')
})

it('每个对象只有一个入口，各原始记录不合并；磁吸有进入/退出阈值且不移动点', () => {
  const rows = [record('A', 10), record('B', 20), { ...record('C', 30), object: 'A' }], before = JSON.stringify(rows)
  expect(replayInspectionObjects(rows).map(row => row.id)).toEqual(['A', 'B'])
  expect(nearestReplayInspection(rows, 19, 10)?.id).toBe('B')
  expect(nearestReplayInspection(rows, 19, 10, 'A')?.id).toBe('A')
  expect(nearestReplayInspection(rows, 29, 10, 'A')?.id).toBe('C')
  expect(nearestReplayInspection(rows, 50, 10)).toBeUndefined()
  expect(nearestReplayInspection([{ ...record('L', 10), radius: 24 }], 32, 10)?.id).toBe('L')
  expect(JSON.stringify(rows)).toBe(before)
})

it('缩小时隐藏碰撞入场点，放大恢复；不移动终点、不影响另一张单或不同价位', () => {
  const marks: OrderMark[] = [{ order, kind: 'placed', time: 50, price: 100 }, { order: { ...order, terminal: true }, kind: 'filled', time: 51, price: 100 }]
  const layout = replayChartLayout(900, 300), view = { min: 99, max: 101, tick: .01 }, before = JSON.stringify(marks)
  expect(replayVisibleOrderMarks(marks, { start_us: 0, end_us: 100 }, view, layout)).toEqual([marks[1]])
  expect(replayVisibleOrderMarks(marks, { start_us: 49, end_us: 52 }, view, layout)).toEqual(marks)
  expect(replayVisibleOrderMarks([marks[0], { ...marks[1], price: 101 }], { start_us: 0, end_us: 100 }, view, layout)).toHaveLength(2)
  expect(replayVisibleOrderMarks([marks[0], { ...marks[1], order: { ...order, key: 'B' } }], { start_us: 0, end_us: 100 }, view, layout)).toHaveLength(2)
  expect(JSON.stringify(marks)).toBe(before)
})
