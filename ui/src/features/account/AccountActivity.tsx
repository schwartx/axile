import { useCallback, useId, useRef, useState, type ReactNode } from 'react'
import { RefreshCw } from 'lucide-react'
import { Segmented } from '@/components/ui/Segmented'
import { OverflowText } from '@/components/ui/OverflowText'
import { activityEvents, type ActivityTab } from '@/features/account/accountActivityModel'
import { orderDirection, orderOffset } from '@/features/account/currentOrdersModel'
import type { OrdersState } from '@/features/account/useCurrentOrders'
import { getAccountActivity, type AccountActivity } from '@/lib/api/orders'
import { usePolling } from '@/lib/hooks/usePolling'
import { useRemountFade } from '@/lib/viewTransition'
import { shanghaiTime } from '@/features/account/executionValues'
import type { ChannelCapability } from '@/types/api'

const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })
const time = (value: string | null) => {
  if (!value) return '未知'
  const timestamp = shanghaiTime(value)
  return Number.isFinite(timestamp) ? new Date(timestamp + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ') : '未知'
}
const columns = 'grid grid-cols-[145px_minmax(90px,1fr)_80px_100px_100px_110px] gap-3'

function ActivityTable({ data, tab, quantityLabel }: { data: AccountActivity; tab: ActivityTab; quantityLabel: string }) {
  const qty = (value: number) => `${number(value)}${quantityLabel ? ` ${quantityLabel}` : ''}`
  if (tab === 'failures') {
    const events = activityEvents(data)
    return events.length ? <div className="divide-y divide-line">{events.map(event => <div key={event.key} className="grid grid-cols-[145px_minmax(0,1fr)] gap-x-4 gap-y-1 py-3 text-[13px]">
      <span className="num text-xs text-ink-3"><small className="block">{event.timeLabel}</small>{time(event.time)}</span>
      <div className="min-w-0"><span className="font-medium">{event.symbol || '账户通知'}</span><span className={`ml-3 ${event.warning ? 'text-warn' : 'text-ink-2'}`}>{event.label}</span>
        <p className="mt-1 break-words text-ink-2">{event.message}</p>{event.orderId && <p className="mt-1 break-all text-xs text-ink-3">订单编号 {event.orderId}</p>}</div>
    </div>)}</div> : <Empty text="当前查询范围内暂无撤单或拒绝记录" />
  }
  const trades = tab === 'trades'
  const rows = trades ? data.trades : data.orders
  if (!rows.length) return <Empty text={trades ? '当前查询范围内暂无成交记录' : '当前查询范围内暂无委托记录'} />
  return <div className="overflow-x-auto"><div className="min-w-[700px]">
    <div className={`${columns} border-b border-line py-2 text-xs text-ink-3`}><span>{trades ? '成交时间' : '委托时间'}</span><span>品种</span><span>买卖 / 开平</span><span>{trades ? '成交价' : '委托价'}</span><span>{trades ? '成交量' : '委托 / 已成'}</span><span>{trades ? '订单编号' : '状态'}</span></div>
    {trades ? data.trades.map(trade => <div key={JSON.stringify([trade.symbol, trade.trade_id, trade.time])} className={`${columns} items-center border-b border-line py-3 text-[13px]`}>
      <span className="num text-xs text-ink-3">{time(trade.time)}</span><OverflowText text={trade.symbol} className="min-w-0 font-medium" />
      <span>{orderDirection(trade.direction)}<small className="block text-ink-3">{orderOffset(trade.offset)}</small></span><span className="num">{number(trade.price)}</span><span className="num">{qty(trade.volume)}</span><OverflowText text={trade.order_id} className="min-w-0 text-xs text-ink-3" />
    </div>) : data.orders.map(order => <div key={JSON.stringify([order.symbol, order.order_id])} className="border-b border-line">
      <div className={`${columns} items-center py-3 text-[13px]`}><span className="num text-xs text-ink-3">{time(order.create_time)}</span><OverflowText text={order.symbol} className="min-w-0 font-medium" />
        <span>{orderDirection(order.direction)}<small className="block text-ink-3">{orderOffset(order.offset)}</small></span><span className="num">{['MARKET', '市价'].includes(order.order_type) ? '市价' : number(order.price)}</span>
        <span className="num">{qty(order.volume)}<small className="block text-ink-3">已成 {qty(order.filled_volume)}</small></span><span className={order.status === '已拒绝' || order.uncertain ? 'text-warn' : 'text-ink-2'}>{order.status}</span></div>
      <p className="pb-2 text-xs break-all text-ink-3">订单编号 {order.order_id}{order.reason ? ` · ${order.reason}` : ''}</p>
    </div>)}
  </div></div>
}

function Empty({ text }: { text: string }) {
  return <p className="py-10 text-center text-sm text-ink-3">{text}</p>
}

export function AccountActivityContent({ state, units, pendingContent, tab, onTabChange }: { state: OrdersState; units?: ChannelCapability['units']; pendingContent: ReactNode; tab: ActivityTab; onTabChange: (value: ActivityTab) => void }) {
  const filterId = useId()
  const [filter, setFilter] = useState({ accountId: state.accountId, value: '' })
  const symbol = filter.accountId === state.accountId ? filter.value : ''
  const [limit, setLimit] = useState(100)
  const request = useRef({ key: '', refresh: true })
  const key = `${state.accountId}:${state.query.data?.instance_id ?? ''}:${symbol}`
  const fetcher = useCallback(async (signal: AbortSignal) => {
    const refresh = request.current.key !== key || request.current.refresh
    const result = await getAccountActivity(state.accountId, symbol, limit, refresh, signal)
    if (!signal.aborted) request.current = { key, refresh: false }
    return result
  }, [state.accountId, key, symbol, limit])
  const query = usePolling(fetcher, { queryKey: `${key}:${limit}`, enabled: state.open && state.connectionReady && tab !== 'pending', retainDataWhenDisabled: true, intervalMs: 5000 })
  const fade = useRemountFade(tab)
  const data = query.data
  const chooseSymbol = (value: string) => { setFilter({ accountId: state.accountId, value }); setLimit(100) }
  return <>
    <div className="mb-4 overflow-x-auto"><Segmented size="sm" className="whitespace-nowrap" value={tab} onChange={onTabChange} options={[
      { value: 'trades', label: '最近成交' }, { value: 'orders', label: '最近委托' },
      { value: 'pending', label: '在途委托', badge: state.query.data?.complete ? <span className="num text-ink-3">{state.query.data.count}</span> : undefined },
      { value: 'failures', label: '撤单 / 拒绝' },
    ]} /></div>
    <div key={tab} className={`min-h-[260px] ${fade ? 'panel-fade-in' : ''}`}>
      {tab === 'pending' ? pendingContent : <>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 text-[13px]">
          <form className="flex items-center gap-2" onSubmit={event => { event.preventDefault(); const value = String(new FormData(event.currentTarget).get('symbol') ?? '').trim(); chooseSymbol(value) }}>
            <label className="text-ink-3" htmlFor={filterId}>品种</label><input key={key} id={filterId} name="symbol" defaultValue={symbol} list={`${filterId}-symbols`} pattern={'[A-Za-z0-9_.:\\/\\-]+'} maxLength={100} placeholder="全部已观测品种" className="min-h-8 w-40 rounded-md border border-line bg-transparent px-2 text-ink-1 outline-accent" />
            <datalist id={`${filterId}-symbols`}>{data?.symbols.map(value => <option key={value} value={value} />)}</datalist><button type="submit" className="min-h-8 cursor-pointer text-accent">筛选</button>{symbol && <button type="button" onClick={() => chooseSymbol('')} className="min-h-8 cursor-pointer text-ink-3">全部</button>}
          </form>
          <button type="button" disabled={!state.connectionReady || query.loading || query.refreshing} onClick={() => { request.current.refresh = true; void query.refresh() }} className="flex min-h-8 cursor-pointer items-center gap-1.5 text-accent disabled:cursor-default disabled:text-ink-3"><RefreshCw size={14} aria-hidden />{query.refreshing || query.loading ? '核对中' : '核对记录'}</button>
        </div>
        {!state.connectionReady && !data && <Empty text="账户连接未就绪，记录暂不可查询" />}
        {query.error && <p role="status" className="mb-3 text-[13px] text-warn">记录读取失败{data ? '，以下为上次读取结果' : '，请稍后重试'}</p>}
        {query.loading && <Empty text="正在读取近期记录…" />}
        {data && <>
          <p className="mb-2 text-xs leading-relaxed text-ink-3">{data.scope}{symbol ? ` · ${symbol}` : ''}</p>
          <p className="mb-3 text-xs text-ink-3">{data.queried_at ? `渠道核对于 ${time(data.queried_at)}` : '尚未核对渠道记录'} · 最多显示最近 {data.limit} 笔</p>
          {data.errors.length > 0 && <div role="status" className="mb-3 text-xs leading-relaxed text-warn">{data.errors.map(message => <p key={message}>{message}</p>)}</div>}
          {tab === 'failures' && <p className="mb-3 text-xs leading-relaxed text-ink-3">操作失败记录自 {time(data.started_at)} 本次连接起采集；撤单失败不表示订单已结束。无法关联的 TQ 通知保留为账户记录。</p>}
          <ActivityTable data={data} tab={tab} quantityLabel={units?.quantity_label ?? ''} />
          {data.truncated && limit < 500 && <button type="button" disabled={query.refreshing} onClick={() => setLimit(value => Math.min(500, value + 100))} className="my-3 min-h-8 cursor-pointer text-[13px] text-accent disabled:text-ink-3">显示更多记录</button>}
        </>}
      </>}
    </div>
  </>
}
