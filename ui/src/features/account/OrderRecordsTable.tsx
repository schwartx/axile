import { useId, useState, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { orderDirection, orderOffset } from '@/features/account/currentOrdersModel'
import { averageText, orderColumns, orderDetails } from '@/features/account/orderColumns'
import type { CurrentOrder } from '@/lib/api/orders'
import type { ChannelCapability } from '@/types/api'
import { useToastStore } from '@/stores/ui'
import { RecordTable, type RecordColumn } from '@/features/account/RecordTable'

export function OrderDetails({ order }: { order: CurrentOrder }) {
  return <>
    <dt>成交均价</dt><dd>{averageText(order)}</dd>
    {order.avg_price != null && <><dt>均价来源</dt><dd>{order.avg_price_source === 'trades' ? '逐笔成交计算' : '渠道返回'}</dd></>}
    {orderDetails(order).map(item => <div key={item.key} className="contents"><dt>{item.label}</dt><dd>{item.text}</dd></div>)}
  </>
}

const detailGrid = 'grid grid-cols-1 gap-x-6 gap-y-2 @xl:grid-cols-2 @4xl:grid-cols-3'

function DetailField({ label, children, wide = false }: { label: string; children: ReactNode; wide?: boolean }) {
  return <div className={`grid min-w-0 grid-cols-[120px_minmax(0,1fr)] gap-x-3 ${wide ? 'col-span-full' : ''}`}>
    <dt className="text-ink-3">{label}</dt><dd className="min-w-0 break-all">{children}</dd>
  </div>
}

function OrderDetailPanel({ order, channel, units, formatTime, copy }: {
  order: CurrentOrder
  channel?: string
  units?: ChannelCapability['units']
  formatTime: Props['formatTime']
  copy: () => Promise<void>
}) {
  const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })
  const fields = orderDetails(order)
  const commonKeys = new Set(['time_in_force', 'cum_quote'])
  const columnKeys = new Set(orderColumns(channel).map(column => column.key))
  const common = fields.filter(field => commonKeys.has(field.key) && !columnKeys.has(field.key))
  const extra = fields.filter(field => !commonKeys.has(field.key) && !columnKeys.has(field.key))
  return <div className="@container pb-4 text-xs text-ink-2">
    <dl className={detailGrid}>
      <DetailField label="订单编号"><span className="inline-flex max-w-full items-start gap-3"><span className="min-w-0 select-text break-all">{order.order_id}</span><button type="button" onClick={() => void copy()} className="shrink-0 cursor-pointer text-accent" aria-label="复制订单编号">复制</button></span></DetailField>
      <DetailField label="更新时间">{formatTime(order.update_time ?? order.create_time)}</DetailField>
      <DetailField label="未成数量">{number(order.remaining_volume)}{units?.quantity_label ? ` ${units.quantity_label}` : ''}</DetailField>
      <DetailField label="关联执行" wide={Boolean(order.execution_id)}>{order.execution_id ?? '未关联'}</DetailField>
      {common.map(field => <DetailField key={field.key} label={field.label}>{field.text}</DetailField>)}
      {order.reason && <DetailField label="状态说明" wide>{order.reason}</DetailField>}
      <DetailField label="记录时间">{formatTime(order.create_time)}</DetailField>
      {order.avg_price != null && <DetailField label="均价来源">{order.avg_price_source === 'trades' ? '逐笔成交计算' : '渠道返回'}</DetailField>}
      {Object.entries(order.native_identity).map(([key, value]) => <DetailField key={key} label={key === 'client_order_id' ? '客户端订单编号' : key} wide>{value}</DetailField>)}
      {extra.map(field => <DetailField key={field.key} label={field.label}>{field.text}</DetailField>)}
    </dl>
  </div>
}

interface Props {
  loading?: boolean
  emptyText?: string
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
    <tr className="h-16 text-[13px] [&_td]:whitespace-nowrap [&_td]:py-3 [&_td]:px-3 [&_td:first-child]:pl-0 [&_td:last-child]:pr-0" data-activity-row>
      <td><time dateTime={order.create_time ?? undefined} className="num text-xs text-ink-3">{formatTime(order.create_time)}</time></td>
      <td className="font-medium"><OverflowText text={order.symbol} className="w-full" /></td>
      <td>{orderDirection(order.direction)}{units?.quantity_kind !== 'share' && <small className="block text-ink-3">{orderOffset(order.offset)}</small>}</td>
      <td className="num text-right" data-numeric><OverflowText text={price} /></td>
      <td className="num text-right" data-numeric><OverflowText text={averageText(order)} /></td>
      <td className="num text-right" data-numeric><OverflowText text={`${number(order.volume)} / ${number(order.filled_volume)}${units?.quantity_label ? ` ${units.quantity_label}` : ''}`} /></td>
      <td className={order.uncertain || status.includes('拒绝') || status.includes('确认') || status.includes('失败') ? 'text-warn' : 'text-ink-2'}><OverflowText text={status} /></td>
      {columns.map(column => <td key={column.key} data-numeric={column.numeric || undefined} className={column.numeric ? 'num text-right' : ''}><OverflowText text={column.text(order)} /></td>)}
      {action && <td>{action(order)}</td>}
      <td className="w-8"><button type="button" aria-expanded={expanded} aria-controls={panel} aria-label={`${expanded ? '收起' : '展开'}委托 ${order.order_id}`}
        onClick={() => setExpanded(value => !value)} className="flex min-h-8 cursor-pointer items-center justify-center text-ink-3 hover:text-ink-1">
        <ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`} />
      </button></td>
    </tr>
    <tr><td colSpan={8 + columns.length + (action ? 1 : 0)} className="p-0">
      <div id={panel} aria-hidden={!expanded} inert={!expanded} className={`grid transition-[grid-template-rows,visibility] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr] visible' : 'grid-rows-[0fr] invisible'}`}>
        <div className="min-h-0 overflow-hidden"><OrderDetailPanel order={order} channel={channel} units={units} formatTime={formatTime} copy={copy} /></div>
      </div>
    </td></tr>
  </tbody>
}

export function OrderRecordsTable(props: Props) {
  const columns = orderColumns(props.channel)
  const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })
  const quantity = (volume: number, filled: number) => `${number(volume)} / ${number(filled)}${props.units?.quantity_label ? ` ${props.units.quantity_label}` : ''}`
  const samples: Record<string, string> = { trading_day: '2026-12-28', hedge_flag: '投机/套保', conditions: '立即成交剩余撤销 / 全部数量', filled_amount: '999,999.99', order_business: '普通交易', native_order_type: '止损限价' }
  const layout: RecordColumn[] = [
    { key: 'time', label: '委托时间', sample: '2026-12-28 23:59:59' },
    { key: 'symbol', label: '品种', sample: 'ETHUSDT', values: props.orders.map(order => order.symbol), maxWidth: 160 },
    { key: 'direction', label: props.units?.quantity_kind === 'share' ? '买卖' : '买卖 / 开平' },
    { key: 'price', label: '委托价', sample: '9,999.99999999', values: props.orders.map(order => number(order.price)), numeric: true },
    { key: 'average', label: '成交均价', sample: '9,999.99999999', values: props.orders.map(averageText), numeric: true },
    { key: 'volume', label: '委托 / 已成', sample: quantity(99999, 99999), values: props.orders.map(order => quantity(order.volume, order.filled_volume)), numeric: true },
    { key: 'status', label: '状态', sample: '部分成交', values: props.orders.map(order => props.statusText?.(order) ?? order.status) },
    ...columns.map(column => ({ key: column.key, label: column.label, numeric: column.numeric, sample: samples[column.key], values: props.orders.map(column.text), maxWidth: column.key === 'conditions' ? 280 : undefined })),
    ...(props.action ? [{ key: 'action', label: '操作', sample: '撤单' }] : []), { key: 'details', label: '详情' },
  ]
  return <RecordTable columns={layout} kind="order" loading={props.loading} empty={!props.orders.length} emptyText={props.emptyText ?? '暂无委托记录'}>
    {props.orders.map(order => <Row key={JSON.stringify([order.symbol, order.order_id])} {...props} order={order} />)}
  </RecordTable>
}
