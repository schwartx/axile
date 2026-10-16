import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { orderDirection } from '@/features/account/currentOrdersModel'
import type { AccountTrade, CurrentOrder } from '@/lib/api/orders'
import { tradeColumns as channelTradeColumns, type TradeColumn } from '@/features/account/tradeColumns'
import { OrderRecordsTable } from '@/features/account/OrderRecordsTable'
import { TradeDetailPanel } from '@/features/account/TradeDetailPanel'
import { RecordTable, type RecordColumn } from '@/features/account/RecordTable'
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
    <tr className="h-16 text-[13px] [&_td]:whitespace-nowrap [&_td]:py-3 [&_td]:px-3 [&_td:first-child]:pl-0 [&_td:last-child]:pr-0" data-activity-row>
      <td><time dateTime={trade.time} className="num text-xs text-ink-3">{formatTime(trade.time)}</time></td>
      <td className="font-medium"><OverflowText text={trade.symbol} className="min-w-0 w-full" /></td>
      <td>{orderDirection(trade.direction)}</td>
      <td className="num text-right" data-numeric><OverflowText text={number(trade.price)} /></td>
      <td className="num text-right" data-numeric><OverflowText text={`${number(trade.volume)}${units?.quantity_label ? ` ${units.quantity_label}` : ''}`} /></td>
      {extraColumns.map(column => <td key={column.key} data-numeric={column.numeric || undefined} className={`${column.numeric ? 'num text-right' : ''} ${column.color?.(trade) ?? ''}`}>
        <OverflowText text={column.text(trade)} />
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

export function ActivityRecordsTable({ trades, orders, units, formatTime, channel, loading, emptyText }: {
  loading?: boolean
  emptyText?: string
  channel?: string
  trades?: AccountTrade[]
  orders?: CurrentOrder[]
  units?: ChannelCapability['units']
  formatTime: (value: string | null) => string
}) {
  const extraColumns = channelTradeColumns(channel)
  const samples: Record<string, string> = { offset: '平今', trading_day: '2026-12-28', hedge_flag: '投机/套保', commission: '0.000000000000', realized_pnl: '-99.99999999', is_maker: 'Maker', position_side: '单向' }
  const columns: RecordColumn[] = [
    { key: 'time', label: '成交时间', sample: '2026-12-28 23:59:59' },
    { key: 'symbol', label: '品种', sample: 'ETHUSDT', values: trades?.map(trade => trade.symbol), maxWidth: 160 },
    { key: 'direction', label: '买卖', sample: '卖出' },
    { key: 'price', label: '成交价', sample: '9,999.99999999', values: trades?.map(trade => number(trade.price)), numeric: true },
    { key: 'volume', label: '成交量', sample: `99,999.9999${units?.quantity_label ? ` ${units.quantity_label}` : ''}`, values: trades?.map(trade => `${number(trade.volume)}${units?.quantity_label ? ` ${units.quantity_label}` : ''}`), numeric: true },
    ...extraColumns.map(column => ({ key: column.key, label: column.label, numeric: column.numeric, sample: samples[column.key] ?? (column.numeric ? '999,999.99' : ''), values: trades?.map(column.text) })),
    { key: 'details', label: '详情' },
  ]
  if (trades) return <RecordTable columns={columns} kind="trade" loading={loading} empty={!trades.length} emptyText={emptyText ?? '暂无成交记录'}>
    {trades.map(trade => <TradeRow key={JSON.stringify(['trade', trade.symbol, trade.trade_id, trade.time])} trade={trade} channel={channel} units={units} formatTime={formatTime} extraColumns={extraColumns} />)}
  </RecordTable>
  return <OrderRecordsTable orders={orders ?? []} channel={channel} units={units} formatTime={formatTime} loading={loading} emptyText={emptyText} />
}
