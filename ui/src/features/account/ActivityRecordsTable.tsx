import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { orderDirection } from '@/features/account/currentOrdersModel'
import type { AccountTrade, CurrentOrder } from '@/lib/api/orders'
import { tradeColumns as channelTradeColumns, type TradeColumn } from '@/features/account/tradeColumns'
import { OrderRecordsTable } from '@/features/account/OrderRecordsTable'
import { useToastStore } from '@/stores/ui'
import type { ChannelCapability } from '@/types/api'

const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })

function TradeRow({ trade, units, formatTime, extraColumns }: {
  extraColumns: TradeColumn[]
  trade: AccountTrade
  units?: ChannelCapability['units']
  formatTime: (value: string | null) => string
}) {
  const [expanded, setExpanded] = useState(false)
  const panelId = useId()
  const toast = useToastStore(state => state.toast)
  const copy = async () => {
    try { await navigator.clipboard.writeText(trade.order_id); toast('订单编号已复制') }
    catch { toast('复制失败，请选中编号手动复制') }
  }
  return <tbody className="border-b border-line" data-activity-record>
    <tr className="h-16 text-[13px] [&_td]:whitespace-nowrap [&_td]:py-3 [&_td]:pl-6 [&_td:first-child]:pl-0 [&_td:last-child]:pl-3" data-activity-row>
      <td><time dateTime={trade.time} className="num text-xs text-ink-3">{formatTime(trade.time)}</time></td>
      <td className="font-medium">{trade.symbol}</td>
      <td>{orderDirection(trade.direction)}</td>
      <td className="num text-right" data-numeric>{number(trade.price)}</td>
      <td className="num text-right" data-numeric>{number(trade.volume)}{units?.quantity_label ? ` ${units.quantity_label}` : ''}</td>
      {extraColumns.map(column => <td key={column.key} data-numeric={column.numeric || undefined} className={`${column.numeric ? 'num text-right' : ''} ${column.color?.(trade) ?? ''}`}>
        {column.truncate ? <OverflowText text={column.text(trade)} className="w-40" /> : column.text(trade)}
      </td>)}
      <td className="w-8"><button type="button" aria-expanded={expanded} aria-controls={panelId} aria-label={`${expanded ? '收起' : '展开'}成交 ${trade.trade_id}`}
        onClick={() => setExpanded(value => !value)} className="flex min-h-8 cursor-pointer items-center justify-center text-ink-3 hover:text-ink-1">
        <ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`} />
      </button></td>
    </tr>
    <tr><td colSpan={6 + extraColumns.length} className="p-0">
      <div id={panelId} aria-hidden={!expanded} inert={!expanded} className={`grid transition-[grid-template-rows,visibility] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr] visible' : 'grid-rows-[0fr] invisible'}`}>
        <div className="min-h-0 overflow-hidden"><dl className="grid grid-cols-[80px_minmax(0,1fr)] gap-x-3 gap-y-2 pb-4 text-xs text-ink-2 [&_dt]:text-ink-3 [&_dd]:min-w-0 [&_dd]:break-all">
          <dt>订单编号</dt><dd className="flex items-start gap-3"><span className="select-text">{trade.order_id || '未知'}</span>{trade.order_id && <button type="button" onClick={() => void copy()} className="shrink-0 cursor-pointer text-accent" aria-label="复制订单编号">复制</button>}</dd>
          <dt>成交编号</dt><dd>{trade.trade_id}</dd><dt>记录时间</dt><dd>{formatTime(trade.time)}</dd>
        </dl></div>
      </div>
    </td></tr>
  </tbody>
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
    {trades.map(trade => <TradeRow key={JSON.stringify(['trade', trade.symbol, trade.trade_id, trade.time])} trade={trade} units={units} formatTime={formatTime} extraColumns={extraColumns} />)}
  </table></div>
  return <OrderRecordsTable orders={orders ?? []} channel={channel} units={units} formatTime={formatTime} />
}
