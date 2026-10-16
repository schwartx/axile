import type { AccountActivity, CurrentOrder, OperationFailure } from '@/lib/api/orders'

export type ActivityTab = 'trades' | 'orders' | 'pending' | 'failures'

export interface ActivityEvent {
  key: string
  time: string | null
  timeLabel: string
  symbol: string
  orderId: string | null
  label: string
  message: string
  warning: boolean
}

export function activityEvents(data: AccountActivity): ActivityEvent[] {
  const ended = (data.ended_orders ?? data.orders).filter(order => ['已撤销', '已拒绝', '已过期'].includes(order.status))
    .map((order: CurrentOrder): ActivityEvent => ({
      key: JSON.stringify(['order', order.symbol, order.order_id]), time: order.create_time, timeLabel: '委托于',
      symbol: order.symbol, orderId: order.order_id, label: order.status,
      message: order.reason ?? `已成 ${order.filled_volume} / 委托 ${order.volume}`,
      warning: order.status === '已拒绝',
    }))
  const failures = data.failures.map((event: OperationFailure): ActivityEvent => ({
    key: `failure:${event.event_id}`, time: event.time, timeLabel: '记录于', symbol: event.symbol, orderId: event.order_id,
    label: event.outcome === 'rejected' ? (event.operation === 'cancel_order' ? '撤单被拒绝' : '下单被拒绝') : event.operation === 'cancel_order' ? '撤单失败 / 待确认' : '下单失败 / 待确认',
    message: event.message, warning: true,
  }))
  return [...ended, ...failures].sort((a, b) => (b.time ?? '').localeCompare(a.time ?? '') || a.key.localeCompare(b.key))
}
