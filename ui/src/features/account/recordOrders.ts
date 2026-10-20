import type { ExecOrder } from '@/types/api'

const dict = (v: unknown): Record<string, unknown> => v && typeof v === 'object' ? v as Record<string, unknown> : {}
const num = (v: unknown): number | null => typeof v === 'number' && Number.isFinite(v) ? v : null

/** 把结果中的原始订单和成交关联为详情证据，不推断未知订单状态。 */
export function recordOrders(result: Record<string, unknown>): ExecOrder[] {
  const trades = Array.isArray(result.trades) ? result.trades.map(dict) : []
  return (Array.isArray(result.orders) ? result.orders : []).map(value => {
    const order = dict(value)
    const extra = dict(order.extra)
    const associated = trades.filter(trade => trade.order_id === order.order_id)
    return {
      order_id: String(order.order_id ?? ''), side: order.direction === '买入' || order.direction === 'BUY' ? 'buy' : order.direction === '卖出' || order.direction === 'SELL' ? 'sell' : 'none',
      order_type: String(order.order_type ?? ''), price: num(order.price), avg_price: num(order.avg_price),
      volume: num(order.volume), filled_volume: num(order.filled_volume), status: String(order.status ?? ''),
      client_order_id: typeof extra.client_order_id === 'string' && extra.client_order_id ? extra.client_order_id : typeof extra.cl_ord_id === 'string' ? extra.cl_ord_id : null,
      trades: associated.map(trade => ({ price: num(trade.trade_price), volume: num(trade.trade_volume),
        value: num(trade.trade_value), time: typeof trade.trade_time === 'string' ? trade.trade_time : null,
        fee: num(dict(trade.extra).commission), fee_asset: typeof dict(trade.extra).commission_asset === 'string' ? String(dict(trade.extra).commission_asset) : null })),
    }
  })
}
