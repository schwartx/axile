import { useId, useState } from 'react'
import { ChevronDown, RefreshCw } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { OverflowText } from '@/components/ui/OverflowText'
import type { CancelReceipt, CurrentOrder } from '@/lib/api/orders'
import { canCancelOrder, orderDirection, orderOffset, orderState, orderSummary } from '@/features/account/currentOrdersModel'
import type { OrdersState } from '@/features/account/useCurrentOrders'
import type { ChannelCapability } from '@/types/api'
import { useErrorPresentation } from '@/lib/errorPresentation'

export function CurrentOrdersButton({ state }: { state: OrdersState }) {
  const failure = useErrorPresentation(state.query.error, { intent: 'uncertain' })
  return <button type="button" aria-haspopup="dialog" onClick={state.show}
    className={`inline-flex min-h-8 flex-none cursor-pointer items-center rounded-md border border-line px-2.5 text-[13px] whitespace-nowrap hover:bg-fill focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${failure.local || state.query.data?.complete === false ? 'text-warn' : 'text-ink-2'}`}>
    <InkRewrite text={state.query.loading ? '当前委托 · 查询中' : orderSummary(state.query.data, state.query.stale)} tone="label" />
  </button>
}

function orderQueryUncertain(state: OrdersState): boolean {
  return state.query.stale || !!state.query.error || state.query.refreshing || state.query.data?.complete === false
}

function OrderRow({ order, receipt, state, quantityLabel, compact }: { order: CurrentOrder; receipt?: CancelReceipt; state: OrdersState; quantityLabel: string; compact?: boolean }) {
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
      <span className={uncertain ? 'text-warn' : 'text-ink-2'}><InkRewrite text={orderState(order, receipt)} tone="label" /></span>
      <button type="button" aria-label={`撤单 ${order.symbol} ${order.order_id}`} disabled={!canCancelOrder(order, receipt, orderQueryUncertain(state))}
        onClick={() => void state.cancel(order)} className="min-h-8 cursor-pointer text-accent hover:underline disabled:cursor-default disabled:text-ink-3 disabled:no-underline">撤单</button>
    </div>
    <div id={panel} inert={!expanded} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden"><dl className="grid grid-cols-[90px_minmax(0,1fr)] gap-x-3 gap-y-1.5 pb-4 pl-5 text-[12.5px] text-ink-2 [&_dd]:break-all [&_dt]:text-ink-3">
        <dt>订单编号</dt><dd>{order.order_id}</dd><dt>下单时间</dt><dd>{order.create_time?.replace('T', ' ') ?? '未知'}</dd>
        <dt>剩余数量</dt><dd>{qty(order.remaining_volume)}{order.uncertain ? '（最后已知值）' : ''}</dd>
        <dt>关联执行</dt><dd>{order.execution_id ?? '未关联'}</dd>
        {Object.entries(order.native_identity).map(([key, value]) => <div key={key} className="contents"><dt>{key}</dt><dd>{value}</dd></div>)}
        {receipt && <><dt>撤单结果</dt><dd>{receipt.message}</dd></>}
      </dl></div>
    </div>
  </div>
}

function ordersSnapshotLabel(state: OrdersState, filtered: boolean, rows: number): string {
  const data = state.query.data
  if (!data) return state.query.loading ? '正在查询账户未完成委托' : '委托状态待确认'
  const count = !data.complete ? '未知' : filtered ? rows : data.count ?? '未知'
  return `${count} 笔 · ${data.sampled_at.replace('T', ' ')} 更新`
}

export function CurrentOrdersDialog({ state, accountName, units }: { state: OrdersState; accountName: string; units?: ChannelCapability['units'] }) {
  return <CenteredModal open={state.open} title="当前委托" subtitle={accountName} onClose={state.close} widthClassName="w-[820px]"
    footer={<div className="flex justify-end border-t border-line px-[22px] py-3"><button type="button" onClick={state.close} className="min-h-9 cursor-pointer rounded-lg border border-line px-4 text-sm text-ink-2">关闭</button></div>}>
    <CurrentOrdersContent state={state} units={units} />
  </CenteredModal>
}

function orderColumns(compact = false): string {
  return compact ? 'grid grid-cols-[minmax(64px,1fr)_56px_64px_74px_74px_36px] gap-2'
    : 'grid grid-cols-[minmax(90px,1fr)_90px_90px_100px_110px_52px] gap-3'
}

function OrdersTable({ rows, state, units, compact }: { rows: CurrentOrder[]; state: OrdersState; units?: ChannelCapability['units']; compact?: boolean }) {
  const receiptOf = (order: CurrentOrder) => state.receipts.find(r => r.order_id === order.order_id && r.symbol === order.symbol)
  return <div className="overflow-x-auto"><div className={compact ? 'min-w-[408px]' : 'min-w-[650px]'}>
    <div className={`${orderColumns(compact)} py-2 text-[12px] text-ink-3`}><span>合约</span><span>{compact ? '买卖/开平' : '买卖 / 开平'}</span><span>委托价</span><span>委托 / 已成</span><span>状态</span><span>操作</span></div>
    {rows.map(order => <OrderRow key={order.order_id} order={order} receipt={receiptOf(order)} state={state} quantityLabel={units?.quantity_label ?? ''} compact={compact} />)}
  </div></div>
}

function OrdersQueryStatus({ state }: { state: OrdersState }) {
  const data = state.query.data
  const failure = useErrorPresentation(state.query.error, { intent: 'uncertain' })
  return <>
    <ErrorNotice intent="uncertain" title="当前委托查询失败，状态待确认" error={state.query.error} embedded onRetry={state.refresh} />
    {data?.complete === false && <p role="status" className="mb-3 text-[13px] text-warn">部分订单终态尚未确认，保留最后已知数量。</p>}
    {state.query.stale && <p role="status" className={`mb-3 text-[13px] ${failure.local ? 'text-warn' : 'text-ink-3'}`}>当前显示最后一次查询结果，请刷新核对。</p>}
    {state.query.loading && <p role="status" className="py-6 text-sm text-ink-3">当前委托查询中…</p>}
  </>
}

/** 账户全部委托与品种弹窗共用同一份查询、回执及撤单逻辑。 */
export function CurrentOrdersContent({ state, units, symbolFilter, compact = false }: {
  state: OrdersState
  units?: ChannelCapability['units']
  symbolFilter?: (symbol: string) => boolean
  compact?: boolean
}) {
  const data = state.query.data
  const rows = data?.data.filter(order => !symbolFilter || symbolFilter(order.symbol)) ?? []
  const feedback = state.receipts.filter(r => (!symbolFilter || symbolFilter(r.symbol)) && (r.state === 'terminal' || r.state === 'failed' || r.state === 'unknown')).slice(-5)
  return <>
    <div className="mb-3 flex items-center justify-between gap-3 text-[12.5px] text-ink-3">
      <span>{ordersSnapshotLabel(state, !!symbolFilter, rows.length)}</span>
      <button type="button" aria-label="刷新当前委托" disabled={state.query.loading || state.query.refreshing} onClick={() => void state.refresh()}
        className="flex min-h-8 cursor-pointer items-center gap-1.5 text-accent disabled:cursor-default disabled:text-ink-3"><RefreshCw size={14} aria-hidden />刷新</button>
    </div>
    <OrdersQueryStatus state={state} />
    {data && rows.length === 0 && data.complete && !state.query.stale && <p className={compact ? 'py-2 text-[13px] text-ink-3' : 'py-8 text-center text-sm text-ink-3'}>{symbolFilter ? '该品种当前无在途委托' : '当前无未完成委托'}</p>}
    {rows.length > 0 && <OrdersTable rows={rows} state={state} units={units} compact={compact} />}
    {feedback.length > 0 && <div className="mt-3 border-t border-line py-3 text-[12.5px]"><p className="mb-1 text-ink-3">最近撤单结果</p>{feedback.map(r => <p key={r.request_id} className={`py-1 ${r.state === 'terminal' ? 'text-ink-2' : 'text-warn'}`}>{r.symbol} · {r.message}{r.order && r.state === 'terminal' ? ` · 已成 ${r.order.filled_volume} / ${r.order.volume}` : ''}</p>)}</div>}
    {!compact && <p className="py-3 text-[12px] text-ink-3">撤单只处理这笔委托的未成交部分。算法运行期间可能继续提交新委托。</p>}
  </>
}
