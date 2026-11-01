import { replayOnBookOrder, replayPriceY, replayTimeX, type ReplayChartLayout } from '@/features/replay/chartGeometry'
import type { ReplayCursor, ReplayOrder, ReplayProjection, ReplayRange, ReplayTrade } from '@/features/replay/types'
import type { ReplayPriceView } from '@/features/replay/chartGeometry'

export const replayTradeKey = (trade: ReplayTrade) => `${trade.capture_seq}:${trade.key}`

export function replayOrderHistoryLabel(order: ReplayOrder, cursor: ReplayCursor): string {
  const future = order.time_us > cursor.time_us || order.time_us === cursor.time_us && cursor.capture_seq != null && order.capture_seq > cursor.capture_seq
  return `${future ? '后续' : ''}已录${order.terminal ? '终态' : '轨迹'}`
}

export interface ReplayOrderLayers { working: boolean; canceled: boolean; filled: boolean }

/** 图层按已读取范围的最后状态筛选整条轨迹，不改变游标状态或原始事实。 */
export function replayVisibleOrders(history: ReplayOrder[], layers: ReplayOrderLayers): ReplayOrder[] {
  const latest = new Map<string, ReplayOrder>()
  for (const order of history) {
    const previous = latest.get(order.key)
    if (!previous || order.time_us > previous.time_us || order.time_us === previous.time_us && order.capture_seq > previous.capture_seq) latest.set(order.key, order)
  }
  return history.filter(order => {
    const last = latest.get(order.key)!
    return last.status === 'filled' ? layers.filled : last.status === 'canceled' ? layers.canceled : replayOnBookOrder(last) ? layers.working : true
  })
}

/** 浏览范围只覆盖当前已读取窗口，不改变回放游标。 */
export function clampReplayView(view: ReplayRange, bounds: ReplayRange): ReplayRange {
  const duration = Math.max(1, Math.min(bounds.end_us - bounds.start_us, Math.round(view.end_us - view.start_us)))
  const start = Math.max(bounds.start_us, Math.min(bounds.end_us - duration, Math.round(view.start_us)))
  return { start_us: start, end_us: start + duration }
}

export function zoomReplayTime(view: ReplayRange, factor: number, anchor: number, bounds: ReplayRange): ReplayRange {
  const duration = Math.max(Math.min(1000, bounds.end_us - bounds.start_us), (view.end_us - view.start_us) * factor)
  const ratio = (anchor - view.start_us) / (view.end_us - view.start_us)
  return clampReplayView({ start_us: anchor - duration * ratio, end_us: anchor + duration * (1 - ratio) }, bounds)
}

export function zoomReplayPriceAt(view: ReplayPriceView, factor: number, anchor: number): ReplayPriceView {
  const span = Math.max(view.tick * 4, (view.max - view.min) * factor)
  const ratio = (anchor - view.min) / (view.max - view.min)
  return { ...view, min: anchor - span * ratio, max: anchor + span * (1 - ratio) }
}

export function replayOrderCommands(order: ReplayOrder, projection: ReplayProjection) {
  const linked = projection.commands.filter(command => command.order_id === order.order_id && command.symbol === order.symbol)
  const ids = new Set(linked.map(command => command.command_id).filter(Boolean))
  return projection.commands.filter(command => linked.includes(command) || ids.has(command.command_id))
}

/** 仅用游标前已关联的请求与回报展开过程；不向未来查询委托身份。 */
export function replayOrderFocus(order: ReplayOrder, projection: ReplayProjection, bounds: ReplayRange): ReplayRange {
  const rows = projection.orderHistory.filter(row => row.key === order.key)
  const times = [...rows.map(row => row.time_us), ...projection.ownTrades.filter(trade => trade.order_id === order.order_id && trade.symbol === order.symbol).map(trade => trade.time_us),
    ...replayOrderCommands(order, projection).map(command => command.time_us)]
  if (replayOnBookOrder(order)) times.push(projection.time_us)
  const from = Math.min(order.time_us, ...times), to = Math.max(order.time_us, ...times), pad = Math.max(1000, (to - from) * 0.3)
  return clampReplayView({ start_us: from - pad, end_us: to + pad }, bounds)
}

export type OrderMarkKind = 'placed' | 'changed' | 'filled' | 'canceled' | 'expired' | 'rejected'
export interface OrderMark { order: ReplayOrder; kind: OrderMarkKind; time: number; price: number }
export interface OrderSegment { order: ReplayOrder; start: number; end: number; next: ReplayOrder | undefined; entry: boolean }

/** 同一委托的入场与终点挤在一起时保留终点；放大后恢复入场，原始观察不变。 */
export function replayVisibleOrderMarks(marks: OrderMark[], range: ReplayRange, view: ReplayPriceView, layout: ReplayChartLayout): OrderMark[] {
  const terminals = new Map<string, OrderMark[]>()
  for (const mark of marks) if (['filled', 'canceled', 'expired', 'rejected'].includes(mark.kind)) {
    const rows = terminals.get(mark.order.key) ?? []; rows.push(mark); terminals.set(mark.order.key, rows)
  }
  return marks.filter(mark => mark.kind !== 'changed' && (mark.kind !== 'placed' || !(terminals.get(mark.order.key) ?? []).some(end =>
    Math.hypot(replayTimeX(end.time, range, layout) - replayTimeX(mark.time, range, layout), replayPriceY(end.price, view, layout) - replayPriceY(mark.price, view, layout)) < 13)))
}

function orderMarkKind(order: ReplayOrder, previous: ReplayOrder | undefined, entry: boolean, active: boolean): OrderMarkKind | null {
  if (order.terminal && order.status !== previous?.status && ['filled', 'canceled', 'expired', 'rejected'].includes(order.status ?? '')) return order.status as OrderMarkKind
  if (active && previous && replayOnBookOrder(previous) && previous.price !== order.price) return 'changed'
  return entry ? 'placed' : null
}

/** 只描绘已观察到的生命周期；首个观察已是终态时只给终点，不补挂单起点。 */
export function replayOrderTimeline(history: ReplayOrder[], symbol: string, range: ReplayRange, cursor: ReplayCursor) {
  const groups = new Map<string, ReplayOrder[]>()
  const segments: OrderSegment[] = [], marks: OrderMark[] = []
  for (const order of history) {
    if (order.symbol !== symbol || order.time_us > cursor.time_us || cursor.capture_seq != null && order.capture_seq > cursor.capture_seq) continue
    const rows = groups.get(order.key) ?? []; rows.push(order); groups.set(order.key, rows)
  }
  for (const rows of groups.values()) {
    rows.sort((a, b) => a.time_us - b.time_us || a.capture_seq - b.capture_seq)
    let observedActive = false
    rows.forEach((order, index) => {
      const previous = rows[index - 1], next = rows[index + 1]
      const active = replayOnBookOrder(order)
      const entry = active && !order.carry_in && !observedActive
      if (active) {
        observedActive = true
        const start = Math.max(range.start_us, order.time_us), end = Math.min(range.end_us - 1, cursor.time_us, next?.time_us ?? cursor.time_us)
        if (end >= start) segments.push({ order, start, end, next, entry })
      }
      if (order.price == null || !Number.isFinite(order.price) || order.time_us < range.start_us || order.time_us >= range.end_us) return
      const kind = orderMarkKind(order, previous, entry, active)
      if (kind) marks.push({ order, kind, time: order.time_us, price: order.price })
    })
  }
  return { segments, marks }
}
