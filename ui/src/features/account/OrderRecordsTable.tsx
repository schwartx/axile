import { useId, useState, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { orderDirection, orderOffset } from '@/features/account/currentOrdersModel'
import { averageText, orderColumns, orderDetails } from '@/features/account/orderColumns'
import type { CurrentOrder } from '@/lib/api/orders'
import type { ChannelCapability } from '@/types/api'
import { useToastStore } from '@/stores/ui'

export function OrderDetails({ order }: { order: CurrentOrder }) {
  return <>
    <dt>成交均价</dt><dd>{averageText(order)}</dd>
    {order.avg_price != null && <><dt>均价来源</dt><dd>{order.avg_price_source === 'trades' ? '逐笔成交计算' : '渠道返回'}</dd></>}
    {orderDetails(order).map(item => <div key={item.key} className="contents"><dt>{item.label}</dt><dd>{item.text}</dd></div>)}
  </>
}

interface Props {
  orders: CurrentOrder[]
  channel?: string
  units?: ChannelCapability['units']
  formatTime: (value: string | null) => string
  action?: (order: CurrentOrder) => ReactNode
  statusText?: (order: CurrentOrder) => string
}

function Row({ order, channel, units, formatTime, action, statusText }: Omit<Props, 'orders'> & { order: CurrentOrder }) {
  const [expanded, setExpanded] = useState(false)
  const panel = useId()
  const toast = useToastStore(state => state.toast)
  const columns = orderColumns(channel)
  const status = statusText?.(order) ?? (order.uncertain ? '待确认' : order.status)
  const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })
  const copy = async () => {
    try { await navigator.clipboard.writeText(order.order_id); toast('订单编号已复制') }
    catch { toast('复制失败，请选中编号手动复制') }
  }
  const price = ['MARKET', '市价'].includes(order.order_type) ? '市价' : number(order.price)
  return <tbody className="border-b border-line" data-activity-record>
    <tr className="h-16 text-[13px] [&_td]:whitespace-nowrap [&_td]:py-3 [&_td]:pl-6 [&_td:first-child]:pl-0 [&_td:last-child]:pl-3" data-activity-row>
      <td><time dateTime={order.create_time ?? undefined} className="num text-xs text-ink-3">{formatTime(order.create_time)}</time></td>
      <td className="font-medium"><OverflowText text={order.symbol} className="max-w-40" /></td>
      <td>{orderDirection(order.direction)}{units?.quantity_kind !== 'share' && <small className="block text-ink-3">{orderOffset(order.offset)}</small>}</td>
      <td className="num text-right" data-numeric>{price}</td>
      <td className="num text-right" data-numeric>{averageText(order)}</td>
      <td className="num text-right" data-numeric>{number(order.volume)} / {number(order.filled_volume)}{units?.quantity_label ? ` ${units.quantity_label}` : ''}</td>
      <td className={order.uncertain || status.includes('拒绝') || status.includes('确认') || status.includes('失败') ? 'text-warn' : 'text-ink-2'}>{status}</td>
      {columns.map(column => <td key={column.key} data-numeric={column.numeric || undefined} className={column.numeric ? 'num text-right' : ''}>{column.text(order)}</td>)}
      {action && <td>{action(order)}</td>}
      <td className="w-8"><button type="button" aria-expanded={expanded} aria-controls={panel} aria-label={`${expanded ? '收起' : '展开'}委托 ${order.order_id}`}
        onClick={() => setExpanded(value => !value)} className="flex min-h-8 cursor-pointer items-center justify-center text-ink-3 hover:text-ink-1">
        <ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`} />
      </button></td>
    </tr>
    <tr><td colSpan={8 + columns.length + (action ? 1 : 0)} className="p-0">
      <div id={panel} aria-hidden={!expanded} inert={!expanded} className={`grid transition-[grid-template-rows,visibility] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr] visible' : 'grid-rows-[0fr] invisible'}`}>
        <div className="min-h-0 overflow-hidden"><dl className="grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-2 pb-4 text-xs text-ink-2 [&_dt]:text-ink-3 [&_dd]:min-w-0 [&_dd]:break-all">
          <dt>订单编号</dt><dd className="flex items-start gap-3"><span className="select-text">{order.order_id}</span><button type="button" onClick={() => void copy()} className="cursor-pointer text-accent" aria-label="复制订单编号">复制</button></dd>
          <dt>记录时间</dt><dd>{formatTime(order.create_time)}</dd>
          <dt>未成数量</dt><dd>{number(order.remaining_volume)} {units?.quantity_label}</dd>
          {order.update_time && <><dt>更新时间</dt><dd>{formatTime(order.update_time)}</dd></>}
          {order.reason && <><dt>状态说明</dt><dd>{order.reason}</dd></>}
          <dt>关联执行</dt><dd>{order.execution_id ?? '未关联'}</dd>
          {Object.entries(order.native_identity).map(([key, value]) => <div key={key} className="contents"><dt>{key}</dt><dd>{value}</dd></div>)}
          <OrderDetails order={order} />
        </dl></div>
      </div>
    </td></tr>
  </tbody>
}

export function OrderRecordsTable(props: Props) {
  const columns = orderColumns(props.channel)
  return <div className="overflow-x-auto"><table className="w-full border-collapse text-left" data-order-table>
    <thead><tr className="border-b border-line text-xs text-ink-3 [&_th]:whitespace-nowrap [&_th]:py-2 [&_th]:pl-6 [&_th]:font-normal [&_th:first-child]:pl-0 [&_th:last-child]:pl-3">
      <th>委托时间</th><th>品种</th><th>{props.units?.quantity_kind === 'share' ? '买卖' : '买卖 / 开平'}</th><th className="text-right">委托价</th>
      <th className="text-right">成交均价</th><th className="text-right">委托 / 已成</th><th>状态</th>
      {columns.map(column => <th key={column.key} className={column.numeric ? 'text-right' : ''}>{column.label}</th>)}
      {props.action && <th>操作</th>}<th><span className="sr-only">详情</span></th>
    </tr></thead>
    {props.orders.map(order => <Row key={JSON.stringify([order.symbol, order.order_id])} {...props} order={order} />)}
  </table></div>
}
