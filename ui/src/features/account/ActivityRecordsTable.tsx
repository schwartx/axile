import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { orderDirection } from '@/features/account/currentOrdersModel'
import type { AccountTrade, CurrentOrder } from '@/lib/api/orders'
import { tradeColumns as channelTradeColumns, type TradeColumn } from '@/features/account/tradeColumns'
import { OrderRecordsTable } from '@/features/account/OrderRecordsTable'
import { TradeDetailPanel } from '@/features/account/TradeDetailPanel'
import type { ChannelCapability } from '@/types/api'

const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })

function TradeRow({ trade, channel, units, formatTime, extraColumns }: {
  channel?: string
  extraColumns: TradeColumn[]
  trade: AccountTrade
  units?: ChannelCapability['units']
  formatTime: (value: string | null) => string
}) {
  const [expanded, setExpanded] = useState(false)
  const panelId = useId()
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
        <div className="sticky left-0 min-h-0 min-w-0 max-w-[calc(92vw-44px)] overflow-hidden"><TradeDetailPanel trade={trade} channel={channel} formatTime={formatTime} /></div>
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
    {trades.map(trade => <TradeRow key={JSON.stringify(['trade', trade.symbol, trade.trade_id, trade.time])} trade={trade} channel={channel} units={units} formatTime={formatTime} extraColumns={extraColumns} />)}
  </table></div>
  return <OrderRecordsTable orders={orders ?? []} channel={channel} units={units} formatTime={formatTime} />
}
