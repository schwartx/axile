import { replayTradeKey } from '@/features/replay/chartInteraction'
import { replayOwnership } from '@/features/replay/ownership'
import { replayStatusLabels } from '@/features/replay/presentation'
import { replayTradeLiquidity } from '@/features/replay/depthGeometry'
import type { ReplayCursor, ReplayOrder, ReplayTrade } from '@/features/replay/types'

export interface InspectionRecord {
  id: string; object: string; kind: 'order' | 'market' | 'depth'; title: string; headline: string; status: string
  label: string; raw: string; time: number; seq?: number; future: boolean; x: number; y: number; radius?: number
  name?: string; order?: string; trade?: string
}
export interface InspectionFormat {
  executionId: string; cursor: ReplayCursor; unit: string
  price: (value: number | null) => string; quantity: (value: number | null) => string
}
export const replayFutureRecord = (time: number, seq: number, cursor: ReplayCursor) => time > cursor.time_us || time === cursor.time_us && cursor.capture_seq != null && seq > cursor.capture_seq
const side = (direction: string | null) => direction === 'BUY' ? '买' : direction === 'SELL' ? '卖' : '方向未知'

export function inspectReplayOrder(order: ReplayOrder, format: InspectionFormat, x: number, y: number): InspectionRecord {
  const status = `${order.carry_in ? '窗前已存在 · ' : ''}${order.cancel_pending ? '撤单待确认' : replayStatusLabels[order.status ?? 'unknown'] ?? '状态未知'}`
  return { id: `order:${order.recording_id ?? ''}:${order.capture_seq}`, object: `order:${order.key}`, kind: 'order', title: `账户委托 · ${replayOwnership(order, format.executionId).label}`,
    headline: `${side(order.direction)} ${format.quantity(order.volume)} ${format.unit} @ ${format.price(order.price)}`, status: `${status} · 剩余 ${format.quantity(order.remaining_volume)} ${format.unit}`,
    label: `${status} · ${format.price(order.price)} · 剩余 ${format.quantity(order.remaining_volume)}`, raw: `委托 ${order.order_id} · #${order.capture_seq}`,
    name: order.order_id, time: order.time_us, seq: order.capture_seq, future: replayFutureRecord(order.time_us, order.capture_seq, format.cursor), order: order.key, x, y }
}

export function inspectReplayTrade(trade: ReplayTrade, market: boolean, object: string, format: InspectionFormat, x: number, y: number): InspectionRecord {
  const quality = trade.conflict || trade.identity_quality !== 'stable' ? ' · 成交身份未确认' : ''
  return { id: `${market ? 'market' : 'trade'}:${trade.recording_id ?? ''}:${trade.capture_seq}`, object, kind: market ? 'market' : 'order',
    title: market ? '市场成交' : `账户逐笔 · ${replayOwnership(trade, format.executionId).label}`,
    headline: `${side(trade.direction)} ${format.quantity(trade.quantity)} ${format.unit} @ ${format.price(trade.price)}`,
    status: `${market ? trade.granularity === 'aggregate' ? '源聚合成交，不拆逐笔' : trade.granularity ?? '源粒度未知' : replayTradeLiquidity(trade)}${quality}`,
    label: `${market ? '市场记录' : '逐笔成交'} · ${side(trade.direction)} ${format.quantity(trade.quantity)} @ ${format.price(trade.price)}`,
    raw: `${trade.source ?? '来源未知'} · 委托 ${trade.order_id ?? '待关联'} · #${trade.capture_seq}`,
    name: trade.order_id ?? undefined, time: trade.time_us, seq: trade.capture_seq, future: replayFutureRecord(trade.time_us, trade.capture_seq, format.cursor), trade: market ? undefined : replayTradeKey(trade), x, y }
}

/** 同一对象只提供一个入口；原始观察仍逐条保留。 */
export function replayInspectionObjects(records: InspectionRecord[]): InspectionRecord[] {
  const objects = new Map<string, InspectionRecord>()
  for (const record of records) if (!objects.has(record.object)) objects.set(record.object, record)
  return [...objects.values()]
}

/** 吸附只选择记录，不移动记录；退出阈值大于进入阈值，避免邻点抖动。 */
export function nearestReplayInspection(records: InspectionRecord[], x: number, y: number, retained?: string): InspectionRecord | undefined {
  const distance = (record: InspectionRecord) => Math.hypot(record.x - x, record.y - y)
  const previous = records.find(record => record.id === retained && distance(record) <= Math.max(18, (record.radius ?? 0) + 4))
  return previous ?? records.filter(record => distance(record) <= Math.max(12, record.radius ?? 0)).sort((a, b) => distance(a) - distance(b))[0]
}
