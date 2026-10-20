import type { AccountActivity, CurrentOrder, OperationFailure } from '@/lib/api/orders'

export type ActivityTab = 'positions' | 'trades' | 'orders' | 'pending' | 'failures' | 'operations'

export interface ActivityEvent {
  key: string
  time: string | null
  symbol: string
  orderId: string | null
  label: string
  message: string
  warning: boolean
  operation: string
}

export function endedActivityOrders(data: AccountActivity): CurrentOrder[] {
  return (data.ended_orders ?? data.orders).filter(order => ['已撤销', '已拒绝', '已过期'].includes(order.status))
    .sort((a, b) => (b.create_time ?? '').localeCompare(a.create_time ?? '') || a.order_id.localeCompare(b.order_id))
}

export function operationEvents(data: AccountActivity): ActivityEvent[] {
  return data.failures.map((event: OperationFailure): ActivityEvent => ({
    key: `failure:${event.event_id}`, time: event.time, symbol: event.symbol, orderId: event.order_id,
    label: event.outcome === 'rejected' ? (event.operation === 'cancel_order' ? '撤单被拒绝' : '下单被拒绝') : event.operation === 'cancel_order' ? '撤单失败 / 待确认' : '下单失败 / 待确认',
    message: event.message, warning: true, operation: event.operation === 'cancel_order' ? '撤单' : event.operation === 'place_order' ? '下单' : event.operation,
  })).sort((a, b) => (b.time ?? '').localeCompare(a.time ?? '') || a.key.localeCompare(b.key))
}
