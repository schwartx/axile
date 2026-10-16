import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { orderDirection, orderOffset } from '@/features/account/currentOrdersModel'
import type { AccountTrade, CurrentOrder } from '@/lib/api/orders'
import { useToastStore } from '@/stores/ui'
import type { ChannelCapability } from '@/types/api'

// 两种记录共用列轨道与 64px 主行，剩余空间由品种、价格和数量共同分担。
const columns = 'grid grid-cols-[160px_minmax(120px,1.2fr)_80px_minmax(95px,1fr)_minmax(135px,1.2fr)_80px_32px] gap-3'
const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })

function RecordRow({ trade, order, units, formatTime }: {
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
  const status = order ? (order.uncertain ? '待确认' : order.status) : '已成交'
  const price = order && ['MARKET', '市价'].includes(order.order_type) ? '市价' : number(record.price)
  const kind = trade ? '成交' : '委托'
  const copy = async () => {
    try { await navigator.clipboard.writeText(record.order_id); toast('订单编号已复制') }
    catch { toast('复制失败，请选中编号手动复制') }
  }
  return <div className="border-b border-line" data-activity-record>
    <div className={`${columns} h-16 items-center text-[13px]`} data-activity-row>
      <time dateTime={timestamp ?? undefined} className="num whitespace-nowrap text-xs text-ink-3">{formatTime(timestamp)}</time>
      <OverflowText text={record.symbol} className="min-w-0 font-medium" />
      <span>{orderDirection(record.direction)}{units?.quantity_kind !== 'share' && <small className="block text-ink-3">{orderOffset(record.offset)}</small>}</span>
      <OverflowText text={price} className="num min-w-0 text-right" />
      <OverflowText text={quantity} className="num min-w-0 text-right" />
      <span className={order?.uncertain || status === '已拒绝' ? 'text-warn' : 'text-ink-2'}>{status}</span>
      <button type="button" aria-expanded={expanded} aria-controls={panelId} aria-label={`${expanded ? '收起' : '展开'}${kind} ${trade?.trade_id ?? record.order_id}`}
        onClick={() => setExpanded(value => !value)} className="flex min-h-8 cursor-pointer items-center justify-center text-ink-3 hover:text-ink-1">
        <ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`} />
      </button>
    </div>
    <div id={panelId} aria-hidden={!expanded} inert={!expanded} className={`grid transition-[grid-template-rows,visibility] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr] visible' : 'grid-rows-[0fr] invisible'}`}>
      <div className="min-h-0 overflow-hidden">
        <dl className="grid grid-cols-[80px_minmax(0,1fr)] gap-x-3 gap-y-2 pb-4 text-xs text-ink-2 [&_dt]:text-ink-3 [&_dd]:min-w-0 [&_dd]:break-all">
          <dt>订单编号</dt><dd className="flex items-start gap-3"><span className="select-text">{record.order_id || '未知'}</span>{record.order_id && <button type="button" onClick={() => void copy()} className="shrink-0 cursor-pointer text-accent" aria-label="复制订单编号">复制</button>}</dd>
          {trade && <><dt>成交编号</dt><dd>{trade.trade_id}</dd></>}
          <dt>记录时间</dt><dd>{formatTime(timestamp)}</dd>
          {order && <><dt>未成数量</dt><dd>{number(order.remaining_volume)} {quantityLabel}</dd>{order.update_time && <><dt>更新时间</dt><dd>{formatTime(order.update_time)}</dd></>}{order.reason && <><dt>状态说明</dt><dd className={status === '已拒绝' ? 'text-warn' : ''}>{order.reason}</dd></>}</>}
        </dl>
      </div>
    </div>
  </div>
}

export function ActivityRecordsTable({ trades, orders, units, formatTime }: {
  trades?: AccountTrade[]
  orders?: CurrentOrder[]
  units?: ChannelCapability['units']
  formatTime: (value: string | null) => string
}) {
  return <div className="overflow-x-auto"><div className="min-w-[800px]">
    <div className={`${columns} border-b border-line py-2 text-xs text-ink-3`}>
      <span>{trades ? '成交时间' : '委托时间'}</span><span>品种</span><span>{units?.quantity_kind === 'share' ? '买卖' : '买卖 / 开平'}</span>
      <span className="text-right">{trades ? '成交价' : '委托价'}</span><span className="text-right">{trades ? '成交量' : '委托 / 已成'}</span><span>状态</span><span className="sr-only">详情</span>
    </div>
    {trades ? trades.map(trade => <RecordRow key={JSON.stringify(['trade', trade.symbol, trade.trade_id, trade.time])} trade={trade} units={units} formatTime={formatTime} />)
      : orders?.map(order => <RecordRow key={JSON.stringify(['order', order.symbol, order.order_id])} order={order} units={units} formatTime={formatTime} />)}
  </div></div>
}
