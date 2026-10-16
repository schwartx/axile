import { useCallback, useId, useRef, useState, type ReactNode } from 'react'
import { CircleHelp, RefreshCw } from 'lucide-react'
import { ContentSwap } from '@/components/ui/ContentSwap'
import { Tooltip } from '@/components/ui/Tooltip'
import { Select } from '@/components/ui/Select'
import { Segmented } from '@/components/ui/Segmented'
import { activityEvents, type ActivityTab } from '@/features/account/accountActivityModel'
import { ActivityRecordsTable } from '@/features/account/ActivityRecordsTable'
import type { OrdersState } from '@/features/account/useCurrentOrders'
import { getAccountActivity, type AccountActivity } from '@/lib/api/orders'
import { usePolling } from '@/lib/hooks/usePolling'
import { shanghaiTime } from '@/features/account/executionValues'
import type { ChannelCapability } from '@/types/api'

const time = (value: string | null) => {
  if (!value) return '未知'
  const timestamp = shanghaiTime(value)
  return Number.isFinite(timestamp) ? new Date(timestamp + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ') : '未知'
}

function ActivityTable({ data, tab, units, channel, loading, emptyText }: { data: AccountActivity | null; tab: ActivityTab; units?: ChannelCapability['units']; channel?: string; loading?: boolean; emptyText?: string }) {
  if (tab === 'failures') {
    if (loading) return <div aria-busy="true" aria-label="撤单与拒绝记录" data-record-placeholder>
      <span className="sr-only" role="status">正在读取近期记录…</span>
      {Array.from({ length: 5 }, (_, index) => <div key={index} aria-hidden className="grid h-16 grid-cols-[145px_minmax(0,1fr)] items-center gap-4 border-b border-line"><div className="h-3 w-24 rounded bg-fill" /><div className="h-3 w-2/3 rounded bg-fill" /></div>)}
    </div>
    if (!data) return <Empty text={emptyText ?? '暂无撤单或拒绝记录'} />
    const events = activityEvents(data)
    return events.length ? <div className="divide-y divide-line">{events.map(event => <div key={event.key} className="grid grid-cols-[145px_minmax(0,1fr)] gap-x-4 gap-y-1 py-3 text-[13px]">
      <span className="num text-xs text-ink-3"><small className="block">{event.timeLabel}</small>{time(event.time)}</span>
      <div className="min-w-0"><span className="font-medium">{event.symbol || '账户通知'}</span><span className={`ml-3 ${event.warning ? 'text-warn' : 'text-ink-2'}`}>{event.label}</span>
        <p className="mt-1 break-words text-ink-2">{event.message}</p>{event.orderId && <p className="mt-1 break-all text-xs text-ink-3">订单编号 {event.orderId}</p>}</div>
    </div>)}</div> : <Empty text="暂无撤单或拒绝记录" />
  }
  const trades = tab === 'trades'
  return <ActivityRecordsTable trades={trades ? data?.trades ?? [] : undefined} orders={trades ? undefined : data?.orders ?? []} channel={data?.channel ?? channel} units={units} formatTime={time} loading={loading} emptyText={emptyText} />
}

function Empty({ text }: { text: string }) {
  return <p className="py-10 text-center text-sm text-ink-3">{text}</p>
}

export function AccountActivityContent({ state, units, channel, pendingContent, tab, onTabChange }: { channel?: string; state: OrdersState; units?: ChannelCapability['units']; pendingContent: ReactNode; tab: ActivityTab; onTabChange: (value: ActivityTab) => void }) {
  const filterId = useId()
  const [filter, setFilter] = useState({ accountId: state.accountId, value: '' })
  const symbol = filter.accountId === state.accountId ? filter.value : ''
  const [manual, setManual] = useState(false)
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
  const data = query.data
  const chooseSymbol = (value: string) => { setFilter({ accountId: state.accountId, value }); setLimit(100); setManual(false) }
  const symbols = [...new Set([...(data?.symbols ?? []), ...(symbol ? [symbol] : [])])].sort()
  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="mb-4 shrink-0 overflow-x-auto"><Segmented size="sm" className="whitespace-nowrap" value={tab} onChange={onTabChange} options={[
      { value: 'trades', label: '最近成交' }, { value: 'orders', label: '最近委托' },
      { value: 'pending', label: '在途委托', badge: state.query.data?.complete ? <span className="num text-ink-3">{state.query.data.count}</span> : undefined },
      { value: 'failures', label: '撤单 / 拒绝' },
    ]} /></div>
    <div className="flex min-h-0 flex-1 flex-col">
      <div hidden={tab === 'pending'} className={`mb-3 ${tab === 'pending' ? 'hidden' : 'flex'} shrink-0 flex-wrap items-center justify-between gap-3 text-[13px]`}>
        <div className="min-w-0 max-w-full">
          <div className="flex items-center gap-2">
            <span className="text-ink-3">品种</span>
            <Select value={symbol} ariaLabel="品种" searchable className="min-h-8 w-48 max-w-full justify-between px-2.5 py-1.5 text-[13px]" options={[
              { value: '', label: '全部品种' },
              ...symbols.map(value => ({ value, label: value })),
              { value: '__custom__', label: '输入其他品种…' },
            ]} onChange={value => { if (value === '__custom__') setManual(true); else chooseSymbol(value) }} />
            <Tooltip content={data?.scope || (query.loading ? '正在读取记录范围…' : '记录范围暂不可用')}>
              <button type="button" aria-label="记录范围说明" onClick={event => event.currentTarget.focus()} className="flex min-h-8 w-8 shrink-0 cursor-help items-center justify-center text-ink-3 hover:text-ink-2"><CircleHelp size={15} aria-hidden /></button>
            </Tooltip>
          </div>
          <div className={`grid transition-[grid-template-rows] duration-150 motion-reduce:transition-none ${manual ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`} inert={!manual}>
            <div className="min-h-0 overflow-hidden">
              <form className="flex flex-wrap items-center gap-2 pt-2" onSubmit={event => { event.preventDefault(); const value = String(new FormData(event.currentTarget).get('symbol') ?? '').trim(); chooseSymbol(value) }}>
                <input key={key} id={filterId} aria-label="其他品种代码" name="symbol" defaultValue={symbol} required pattern={'[A-Za-z0-9_.:\\/\\-]+'} maxLength={100} placeholder="输入完整品种代码" className="min-h-8 w-48 rounded-md border border-line bg-transparent px-2 text-ink-1 outline-accent" />
                <button type="submit" className="min-h-8 cursor-pointer text-accent">筛选</button>
                <button type="button" onClick={() => setManual(false)} className="min-h-8 cursor-pointer text-ink-3">取消</button>
              </form>
            </div>
          </div>
        </div>
        <Tooltip content={data?.queried_at ? `上次核对：${time(data.queried_at)}` : '尚未核对渠道记录'}>
        <button type="button" aria-label="核对记录" disabled={!state.connectionReady || query.loading || query.refreshing} onClick={() => { request.current.refresh = true; void query.refresh() }} className="flex min-h-8 min-w-20 cursor-pointer items-center justify-end gap-1.5 text-accent disabled:cursor-default disabled:text-ink-3"><RefreshCw size={14} aria-hidden />{query.refreshing || query.loading ? '核对中' : '核对记录'}</button>
        </Tooltip>
      </div>
      <div className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto [scrollbar-gutter:stable]" data-activity-scroll>
      <ContentSwap identity={tab}>
        {tab === 'pending' ? pendingContent : <>
          {query.error && <p role="status" className="mb-3 text-[13px] text-warn">记录读取失败{data ? '，以下为上次读取结果' : '，请稍后重试'}</p>}
          {data && data.errors.length > 0 && <div role="status" className="mb-3 text-xs leading-relaxed text-warn">{data.errors.map(message => <p key={message}>{message}</p>)}</div>}
          <ActivityTable data={data} tab={tab} units={units} channel={channel} loading={query.loading}
            emptyText={!state.connectionReady && !data ? '账户连接未就绪，记录暂不可查询' : query.error && !data ? '记录暂不可用，请稍后重试' : undefined} />
          {data?.truncated && <p className="my-3 text-xs text-ink-3">每类显示最近 {data.limit} 笔记录</p>}
          {data?.truncated && limit < 500 && <button type="button" disabled={query.refreshing} onClick={() => setLimit(value => Math.min(500, value + 100))} className="my-3 min-h-8 cursor-pointer text-[13px] text-accent disabled:text-ink-3">加载更多</button>}
        </>}
      </ContentSwap>
      </div>
    </div>
  </div>
}
