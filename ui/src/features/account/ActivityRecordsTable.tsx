import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { orderDirection, orderOffset } from '@/features/account/currentOrdersModel'
import type { AccountTrade, CurrentOrder } from '@/lib/api/orders'
import { tradeColumns as channelTradeColumns, type TradeColumn } from '@/features/account/tradeColumns'
import { useToastStore } from '@/stores/ui'
import type { ChannelCapability } from '@/types/api'

// 成交只展示成交事实；委托额外展示状态，各自分配剩余列宽。
const orderColumns = 'grid grid-cols-[160px_minmax(120px,1.2fr)_80px_minmax(95px,1fr)_minmax(135px,1.2fr)_80px_32px] gap-3'
const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })

function RecordRow({ trade, order, units, formatTime, extraColumns }: {
  extraColumns: TradeColumn[]
  trade?: AccountTrade
  order?: CurrentOrder
  units?: ChannelCapability['units']
  formatTime: (value: string | null) => string
}) {
  const record = (trade ?? order)!
  const [expanded, setExpanded] = useState(false)
  const panelId = useId()
  const toast = useToastStore(state => state.toast)
  const quantityLabel = units?.quantity_label ?? ''
  const quantity = `${number(record.volume)}${order ? ` / ${number(order.filled_volume)}` : ''}${quantityLabel ? ` ${quantityLabel}` : ''}`
  const timestamp = trade ? trade.time : order!.create_time
  const status = order ? (order.uncertain ? '待确认' : order.status) : null
  const price = order && ['MARKET', '市价'].includes(order.order_type) ? '市价' : number(record.price)
  const kind = trade ? '成交' : '委托'
  const copy = async () => {
    try { await navigator.clipboard.writeText(record.order_id); toast('订单编号已复制') }
    catch { toast('复制失败，请选中编号手动复制') }
  }
  const expandButton = <button type="button" aria-expanded={expanded} aria-controls={panelId} aria-label={`${expanded ? '收起' : '展开'}${kind} ${trade?.trade_id ?? record.order_id}`}
        onClick={() => setExpanded(value => !value)} className="flex min-h-8 cursor-pointer items-center justify-center text-ink-3 hover:text-ink-1">
        <ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`} />
      </button>
  const details = <div id={panelId} aria-hidden={!expanded} inert={!expanded} className={`grid transition-[grid-template-rows,visibility] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr] visible' : 'grid-rows-[0fr] invisible'}`}>
      <div className="min-h-0 overflow-hidden">
        <dl className="grid grid-cols-[80px_minmax(0,1fr)] gap-x-3 gap-y-2 pb-4 text-xs text-ink-2 [&_dt]:text-ink-3 [&_dd]:min-w-0 [&_dd]:break-all">
          <dt>订单编号</dt><dd className="flex items-start gap-3"><span className="select-text">{record.order_id || '未知'}</span>{record.order_id && <button type="button" onClick={() => void copy()} className="shrink-0 cursor-pointer text-accent" aria-label="复制订单编号">复制</button>}</dd>
          {trade && <><dt>成交编号</dt><dd>{trade.trade_id}</dd></>}
          <dt>记录时间</dt><dd>{formatTime(timestamp)}</dd>
          {order && <><dt>未成数量</dt><dd>{number(order.remaining_volume)} {quantityLabel}</dd>{order.update_time && <><dt>更新时间</dt><dd>{formatTime(order.update_time)}</dd></>}{order.reason && <><dt>状态说明</dt><dd className={status === '已拒绝' ? 'text-warn' : ''}>{order.reason}</dd></>}</>}
        </dl>
      </div>
    </div>
  if (trade) return <tbody className="border-b border-line" data-activity-record>
    <tr className="h-16 text-[13px] [&_td]:whitespace-nowrap [&_td]:py-3 [&_td]:pl-6 [&_td:first-child]:pl-0 [&_td:last-child]:pl-3" data-activity-row>
      <td><time dateTime={timestamp ?? undefined} className="num text-xs text-ink-3">{formatTime(timestamp)}</time></td>
      <td className="font-medium">{trade.symbol}</td>
      <td>{orderDirection(trade.direction)}</td>
      <td className="num text-right" data-numeric>{price}</td>
      <td className="num text-right" data-numeric>{quantity}</td>
      {extraColumns.map(column => <td key={column.key} data-numeric={column.numeric || undefined} className={`${column.numeric ? 'num text-right' : ''} ${column.color?.(trade) ?? ''}`}>
        {column.truncate ? <OverflowText text={column.text(trade)} className="w-40" /> : column.text(trade)}
      </td>)}
      <td className="w-8">{expandButton}</td>
    </tr>
    <tr><td colSpan={6 + extraColumns.length} className="p-0">{details}</td></tr>
  </tbody>
  return <div className="border-b border-line" data-activity-record>
    <div className={`${orderColumns} h-16 items-center text-[13px]`} data-activity-row>
      <time dateTime={timestamp ?? undefined} className="num whitespace-nowrap text-xs text-ink-3">{formatTime(timestamp)}</time>
      <OverflowText text={record.symbol} className="min-w-0 font-medium" />
      <span>{orderDirection(record.direction)}{order && units?.quantity_kind !== 'share' && <small className="block text-ink-3">{orderOffset(record.offset)}</small>}</span>
      <OverflowText text={price} className="num min-w-0 text-right" />
      <OverflowText text={quantity} className="num min-w-0 text-right" />
      {order && <span className={order.uncertain || status === '已拒绝' ? 'text-warn' : 'text-ink-2'}>{status}</span>}
      {expandButton}
    </div>
    {details}
  </div>
}

export function ActivityRecordsTable({ trades, orders, units, formatTime, channel }: {
  channel?: string
  trades?: AccountTrade[]
  orders?: CurrentOrder[]
  units?: ChannelCapability['units']
  formatTime: (value: string | null) => string
}) {
  const extraColumns = channelTradeColumns(channel)

  if (trades) return <div className="overflow-x-auto"><table className="w-full border-collapse text-left" data-trade-table>
    <thead><tr className="border-b border-line text-xs text-ink-3 [&_th]:whitespace-nowrap [&_th]:py-2 [&_th]:pl-6 [&_th]:font-normal [&_th:first-child]:pl-0 [&_th:last-child]:pl-3">
      <th>成交时间</th><th>品种</th><th>买卖</th><th className="text-right">成交价</th><th className="text-right">成交量</th>
      {extraColumns.map(column => <th key={column.key} className={column.numeric ? 'text-right' : ''}>{column.label}</th>)}
      <th className="w-8"><span className="sr-only">详情</span></th>
    </tr></thead>
    {trades.map(trade => <RecordRow key={JSON.stringify(['trade', trade.symbol, trade.trade_id, trade.time])} trade={trade} units={units} formatTime={formatTime} extraColumns={extraColumns} />)}
  </table></div>
  return <div className="overflow-x-auto"><div className="min-w-[800px]">
    <div className={`${orderColumns} border-b border-line py-2 text-xs text-ink-3`}>
      <span>委托时间</span><span>品种</span><span>{units?.quantity_kind === 'share' ? '买卖' : '买卖 / 开平'}</span>
      <span className="text-right">委托价</span><span className="text-right">委托 / 已成</span><span>状态</span><span className="sr-only">详情</span>
    </div>
    {orders?.map(order => <RecordRow key={JSON.stringify(['order', order.symbol, order.order_id])} order={order} units={units} formatTime={formatTime} extraColumns={extraColumns} />)}
  </div></div>
}
