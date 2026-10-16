import type { AccountTrade } from '@/lib/api/orders'
import { orderOffset } from '@/features/account/currentOrdersModel'

export interface TradeDetail { key: string; label: string; text: string; copyable?: boolean }

const labels: Record<string, string> = {
  exchange_id: '交易所', order_sys_id: '交易所委托编号', order_ref: '报单引用',
  event_date: '原生成交日期', event_clock: '原生成交时间', tq_symbol: '完整合约代码',
  gm_symbol: '原生证券代码', cl_ord_id: '客户端订单编号', exchange_order_id: '柜台委托编号',
  client_order_id: '客户端订单编号', execution_type: '成交事件类型', order_status: '成交时委托状态',
}
const eventTypes: Record<string, string> = { TRADE: '成交', CALCULATED: '强平成交' }
const statuses: Record<string, string> = {
  NEW: '待成交', PARTIALLY_FILLED: '部分成交', FILLED: '已成交', CANCELED: '已撤销',
  PENDING_CANCEL: '待撤销', REJECTED: '已拒绝', EXPIRED: '已过期', EXPIRED_IN_MATCH: '撮合时过期',
}
const identifiers = new Set(['order_sys_id', 'order_ref', 'cl_ord_id', 'exchange_order_id', 'client_order_id'])

/** 展示成交记录自身提供的信息，主表字段不重复；缺失项不创建占位行。 */
export function tradeDetails(trade: AccountTrade, channel?: string): TradeDetail[] {
  const fields = Object.entries(labels).flatMap(([key, label]) => {
    const value = trade.attributes?.[key]
    if (!value || ((key === 'cl_ord_id' || key === 'exchange_order_id') && value === trade.order_id)) return []
    const text = key === 'event_date' ? value.replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3')
      : key === 'execution_type' ? eventTypes[value] ?? value
      : key === 'order_status' ? statuses[value] ?? value : value
    return [{ key, label, text, copyable: identifiers.has(key) }]
  })
  if (trade.exchange_trade_id) fields.push({ key: 'exchange_trade_id', label: '交易所成交编号', text: trade.exchange_trade_id, copyable: true })
  if (channel === 'gm' && trade.offset) fields.push({ key: 'offset', label: '开平', text: orderOffset(trade.offset), copyable: false })
  return fields
}
