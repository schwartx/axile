import { useId, useState } from 'react'
import { ChevronDown, RefreshCw } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { ErrorDetailsModal } from '@/components/ui/ErrorDetailsModal'
import { errorInfo } from '@/lib/errorInfo'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { OverflowText } from '@/components/ui/OverflowText'
import type { CancelReceipt, CurrentOrder } from '@/lib/api/orders'
import { canCancelOrder, currentOrderKey, orderDirection, orderOffset, orderState, ordersSnapshotLabel } from '@/features/account/currentOrdersModel'
import type { OrdersState } from '@/features/account/useCurrentOrders'
import type { ChannelCapability } from '@/types/api'
import { shanghaiTime } from '@/features/account/executionValues'
import { OrderRecordsTable, OrderDetails } from '@/features/account/OrderRecordsTable'
import { useErrorPresentation } from '@/lib/errorPresentation'
import { AccountActivityContent } from '@/features/account/AccountActivity'
import type { ActivityTab } from '@/features/account/accountActivityModel'

export function CurrentOrdersButton({ state }: { state: OrdersState }) {
  const snapshot = state.query.data
  const label = snapshot?.complete && snapshot.count != null ? `当日委托与成交 · 在途 ${snapshot.count}` : '当日委托与成交'
  return <button type="button" aria-haspopup="dialog" onClick={state.show}
    className="inline-flex min-h-8 flex-none cursor-pointer items-center rounded-md border border-line px-2.5 text-[13px] whitespace-nowrap text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
    <InkRewrite text={label} tone="label" />
  </button>
}

function orderQueryUncertain(state: OrdersState): boolean {
  return !state.connectionReady || state.query.stale || !!state.query.error || state.query.refreshing || state.query.data?.complete === false
}

function OrderRow({ order: snapshotOrder, receipt, state, quantityLabel, compact }: { order: CurrentOrder; receipt?: CancelReceipt; state: OrdersState; quantityLabel: string; compact?: boolean }) {
  const order = receipt?.state === 'terminal' && receipt.order ? receipt.order : snapshotOrder
  const [expanded, setExpanded] = useState(false)
  const instanceId = useId()
  const panel = `current-order-${encodeURIComponent(order.order_id)}${instanceId}`
  const qty = (value: number) => `${value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })}${quantityLabel ? ` ${quantityLabel}` : ''}`
  const uncertain = order.uncertain || receipt?.state === 'unknown'
  return <div className="border-t border-line">
    <div className={`${orderColumns(compact)} items-center py-3 text-[13px]`}>
      <button type="button" aria-expanded={expanded} aria-controls={panel} aria-label={`${expanded ? '收起' : '展开'}委托 ${order.order_id}`}
        onClick={() => setExpanded(!expanded)} className="flex min-h-8 min-w-0 cursor-pointer items-center gap-1.5 text-left font-medium">
        <ChevronDown size={14} aria-hidden className={`shrink-0 transition-transform motion-reduce:transition-none ${expanded ? 'rotate-180' : '-rotate-90'}`} />
        <OverflowText text={order.symbol} className="min-w-0" />
      </button>
      <span>{orderDirection(order.direction)}<small className="block text-ink-3">{orderOffset(order.offset)}</small></span>
      <span className="num">{order.order_type === 'MARKET' || order.order_type === '市价' ? '市价' : order.price.toLocaleString('zh-CN', { maximumFractionDigits: 8 })}</span>
      <span className="num">{qty(order.volume)}<small className="block text-ink-3">已成 {qty(order.filled_volume)}</small></span>
      <span className={uncertain || receipt?.state === 'failed' ? 'text-warn' : 'text-ink-2'}><InkRewrite text={orderState(order, receipt)} tone="label" /></span>
      <button type="button" aria-label={`撤单 ${order.symbol} ${order.order_id}`} disabled={!canCancelOrder(order, receipt, orderQueryUncertain(state))}
        onClick={() => void state.cancel(order)} className="min-h-8 cursor-pointer text-accent hover:underline disabled:cursor-default disabled:text-ink-3 disabled:no-underline">撤单</button>
    </div>
    <div id={panel} inert={!expanded} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden"><dl className="grid grid-cols-[90px_minmax(0,1fr)] gap-x-3 gap-y-1.5 pb-4 pl-5 text-[12.5px] text-ink-2 [&_dd]:break-all [&_dt]:text-ink-3">
        <dt>订单编号</dt><dd>{order.order_id}</dd><dt>下单时间</dt><dd>{order.create_time?.replace('T', ' ') ?? '未知'}</dd>
        <dt>剩余数量</dt><dd>{qty(order.remaining_volume)}{order.uncertain ? '（最后已知值）' : ''}</dd>
        <dt>关联执行</dt><dd>{order.execution_id ?? '未关联'}</dd>
        {Object.entries(order.native_identity).map(([key, value]) => <div key={key} className="contents"><dt>{key}</dt><dd>{value}</dd></div>)}
        <OrderDetails order={order} />
        {receipt && <><dt>撤单结果</dt><dd>{receipt.message}</dd></>}
      </dl></div>
    </div>
  </div>
}

export function CurrentOrdersDialog({ state, units, channel }: { state: OrdersState; units?: ChannelCapability['units']; channel?: string }) {
  const [selection, setSelection] = useState<{ accountId: number; tab: ActivityTab }>({ accountId: state.accountId, tab: 'pending' })
  const tab = selection.accountId === state.accountId ? selection.tab : 'pending'
  return <CenteredModal open={state.open} title="当日委托与成交" onClose={state.close} widthClassName="w-[1440px]"
    footer={<div className="flex justify-end border-t border-line px-[22px] py-3"><button type="button" onClick={state.close} className="min-h-9 cursor-pointer rounded-lg border border-line px-4 text-sm text-ink-2">关闭</button></div>}>
    <AccountActivityContent state={state} units={units} tab={tab} onTabChange={value => setSelection({ accountId: state.accountId, tab: value })} pendingContent={<CurrentOrdersContent state={state} units={units} channel={channel} />} />
  </CenteredModal>
}

function orderColumns(compact = false): string {
  return compact ? 'grid grid-cols-[minmax(64px,1fr)_56px_64px_74px_74px_36px] gap-2'
    : 'grid grid-cols-[minmax(90px,1fr)_90px_90px_100px_110px_52px] gap-3'
}

function OrdersTable({ rows, state, units, compact, channel }: { rows: CurrentOrder[]; state: OrdersState; units?: ChannelCapability['units']; compact?: boolean; channel?: string }) {
  const receiptOf = (order: CurrentOrder) => state.receipts.find(r => r.order_id === order.order_id && r.symbol === order.symbol)
  if (!compact) return <OrderRecordsTable orders={rows.map(order => { const receipt = receiptOf(order); return receipt?.state === 'terminal' && receipt.order ? receipt.order : order })} channel={channel} units={units}
    formatTime={value => { const timestamp = shanghaiTime(value ?? ''); return value && Number.isFinite(timestamp) ? new Date(timestamp + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ') : '未知' }} statusText={order => orderState(order, receiptOf(order))}
    action={order => <button type="button" aria-label={`撤单 ${order.symbol} ${order.order_id}`} disabled={!canCancelOrder(order, receiptOf(order), orderQueryUncertain(state))}
      onClick={() => void state.cancel(order)} className="min-h-8 cursor-pointer text-accent disabled:cursor-default disabled:text-ink-3">撤单</button>} />
  return <div className="overflow-x-auto"><div className={compact ? 'min-w-[408px]' : 'min-w-[650px]'}>
    <div className={`${orderColumns(compact)} py-2 text-[12px] text-ink-3`}><span>合约</span><span>{compact ? '买卖/开平' : '买卖 / 开平'}</span><span>委托价</span><span>委托 / 已成</span><span>状态</span><span>操作</span></div>
    {rows.map(order => <OrderRow key={currentOrderKey(order)} order={order} receipt={receiptOf(order)} state={state} quantityLabel={units?.quantity_label ?? ''} compact={compact} />)}
  </div></div>
}

function OrdersQueryFailure({ state }: { state: OrdersState }) {
  const error = state.query.refreshError
  const { local } = useErrorPresentation(error, { intent: 'mutation', networkFeedback: 'global-only' })
  const [selectedError, setSelectedError] = useState<unknown>(null)
  return <>
    {local && <button type="button" className="min-h-8 cursor-pointer text-warn" onClick={() => setSelectedError(error)}>核对失败</button>}
    <ErrorDetailsModal selection={local && selectedError === error ? { title: '核对失败', info: errorInfo(error) } : null} onClose={() => setSelectedError(null)} />
  </>
}

function ReceiptResult({ receipt }: { receipt: CancelReceipt }) {
  const [expanded, setExpanded] = useState(false)
  const panel = useId()
  const label = receipt.state === 'terminal' ? receipt.order?.status ?? '撤单已确认' : receipt.state === 'unknown' ? '待确认' : '撤单失败'
  return <div className="py-1">
    <button type="button" aria-expanded={expanded} aria-controls={panel} onClick={() => setExpanded(value => !value)}
      className={`flex min-h-8 cursor-pointer items-center gap-1.5 ${receipt.state === 'terminal' ? 'text-ink-2' : 'text-warn'}`}>
      <ChevronDown size={14} aria-hidden className={`transition-transform motion-reduce:transition-none ${expanded ? 'rotate-180' : '-rotate-90'}`} />
      {receipt.symbol} · {label}
    </button>
    <div id={panel} inert={!expanded} aria-hidden={!expanded} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden"><p className="break-words py-1 text-ink-2">{receipt.message}{receipt.order && receipt.state === 'terminal' ? ` · 已成 ${receipt.order.filled_volume} / ${receipt.order.volume}` : ''}</p></div>
    </div>
  </div>
}

/** 账户全部委托与品种弹窗共用同一份查询、回执及撤单逻辑。 */
export function CurrentOrdersContent({ state, units, symbolFilter, channel, compact = false }: {
  state: OrdersState
  channel?: string
  units?: ChannelCapability['units']
  symbolFilter?: (symbol: string) => boolean
  compact?: boolean
}) {
  const data = state.query.data
  const rows = data?.data.filter(order => !symbolFilter || symbolFilter(order.symbol)) ?? []
  const feedback = state.receipts.filter(r => (!symbolFilter || symbolFilter(r.symbol)) && !rows.some(order => currentOrderKey(order) === currentOrderKey(r)) && (r.state === 'terminal' || r.state === 'failed' || r.state === 'unknown')).slice(-5)
  return <>
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3 text-[12.5px] text-ink-3">
      <span>{ordersSnapshotLabel(data, !!symbolFilter, rows.length)}</span>
      <div className="flex items-center gap-3"><OrdersQueryFailure state={state} /><button type="button" aria-label={'核对在途委托'} disabled={!state.connectionReady || state.query.loading || state.query.refreshing} onClick={() => void state.refresh()}
        className="flex min-h-8 cursor-pointer items-center gap-1.5 text-accent disabled:cursor-default disabled:text-ink-3"><RefreshCw size={14} aria-hidden />{state.query.refreshing ? '核对中' : '核对委托'}</button></div>
    </div>
    {!data?.complete && rows.length === 0 && <div className="py-6 text-center text-ink-3">—</div>}
    {state.connectionReady && data && rows.length === 0 && data.complete && !state.query.stale && <p className={compact ? 'py-2 text-[13px] text-ink-3' : 'py-8 text-center text-sm text-ink-3'}>{symbolFilter ? '该品种当前无在途委托' : '当前无在途委托'}</p>}
    {rows.length > 0 && <OrdersTable rows={rows} state={state} units={units} compact={compact} channel={channel} />}
    {feedback.length > 0 && <div className="mt-3 border-t border-line py-3 text-[12.5px]">{feedback.map(receipt => <ReceiptResult key={receipt.request_id} receipt={receipt} />)}</div>}
  </>
}
