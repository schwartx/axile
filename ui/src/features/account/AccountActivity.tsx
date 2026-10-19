import { useCallback, useId, useRef, useState, type ReactNode } from 'react'
import { CircleHelp, RefreshCw } from 'lucide-react'
import { ContentSwap } from '@/components/ui/ContentSwap'
import { Tooltip } from '@/components/ui/Tooltip'
import { Select } from '@/components/ui/Select'
import { Segmented } from '@/components/ui/Segmented'
import { endedActivityOrders, type ActivityTab } from '@/features/account/accountActivityModel'
import { ActivityRecordsTable } from '@/features/account/ActivityRecordsTable'
import { OperationFailuresTable } from '@/features/account/OperationFailuresTable'
import { OrderRecordsTable } from '@/features/account/OrderRecordsTable'
import type { OrdersState } from '@/features/account/useCurrentOrders'
import { getAccountActivity, type AccountActivity } from '@/lib/api/orders'
import { usePolling } from '@/lib/hooks/usePolling'
import { shanghaiTime } from '@/features/account/executionValues'
import type { ChannelCapability } from '@/types/api'
import { activityView, rememberActivityView, activityPositionKey, type ActivityView } from '@/features/account/activityViewMemory'
import { useActivityPosition } from '@/features/account/useActivityPosition'
import { usePositionRecords } from '@/features/account/usePositionRecords'
import { PositionRecordsTable } from '@/features/account/PositionRecordsTable'
import { ReadStatusDot } from '@/features/account/ReadStatusDot'
import { useReadWarningDelay } from '@/features/account/useReadWarningDelay'

const time = (value: string | null) => {
  if (!value) return '未知'
  const timestamp = shanghaiTime(value)
  return Number.isFinite(timestamp) ? new Date(timestamp + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ') : '未知'
}

function ActivityTable({ data, tab, units, channel, loading, emptyText }: { data: AccountActivity | null; tab: ActivityTab; units?: ChannelCapability['units']; channel?: string; loading?: boolean; emptyText?: string }) {
  if (tab === 'operations') return <OperationFailuresTable data={data} formatTime={time} loading={loading} emptyText={emptyText} />
  if (tab === 'failures') return <OrderRecordsTable showAverage={false} orders={data ? endedActivityOrders(data) : []} channel={data?.channel ?? channel} units={units} formatTime={time} loading={loading} emptyText={emptyText ?? '暂无撤销、拒绝或过期委托'} />
  const trades = tab === 'trades'
  return <ActivityRecordsTable trades={trades ? data?.trades ?? [] : undefined} orders={trades ? undefined : data?.orders ?? []} channel={data?.channel ?? channel} units={units} formatTime={time} loading={loading} emptyText={emptyText} />
}

export function AccountActivityContent({ state, units, channel, pendingContent }: { channel?: string; state: OrdersState; units?: ChannelCapability['units']; pendingContent: ReactNode }) {
  const filterId = useId()
  const [view, setView] = useState(() => activityView(state.accountId))
  const { tab } = view
  const symbol = tab === 'positions' ? view.positionSymbol : tab === 'operations' ? view.operationSymbol : view.symbol
  const limit = tab === 'operations' ? view.operationLimit : view.limit
  const updateView = (patch: Partial<ActivityView>) => {
    const next = { ...view, ...patch }
    rememberActivityView(state.accountId, next)
    setView(next)
  }
  const [manual, setManual] = useState(false)
  const request = useRef({ key: '', refresh: true, manual: false })
  const [reconcileFailure, setReconcileFailure] = useState<{ key: string; message: string } | null>(null)
  const key = `${state.accountId}:${state.query.data?.instance_id ?? ''}:${symbol}`
  const fetcher = useCallback(async (signal: AbortSignal) => {
    const refresh = request.current.key !== key || request.current.refresh
    const manual = request.current.key === key && request.current.manual
    try {
      const result = await getAccountActivity(state.accountId, symbol, limit, refresh, signal)
      if (!signal.aborted) {
        request.current = { key, refresh: false, manual: false }
        if (manual) setReconcileFailure(result.errors.length ? { key: `${key}:${limit}`, message: result.errors.join('；') } : null)
      }
      return result
    } catch (error) {
      if (!signal.aborted && manual) {
        request.current.manual = false
        setReconcileFailure({ key: `${key}:${limit}`, message: error instanceof Error ? error.message : String(error) })
      }
      throw error
    }
  }, [state.accountId, key, symbol, limit])
  const query = usePolling(fetcher, { queryKey: `${key}:${limit}`, enabled: state.open && state.connectionReady && tab !== 'pending' && tab !== 'positions', retainDataWhenDisabled: true, intervalMs: 5000 })
  const holdings = usePositionRecords(state.accountId, state.query.data?.instance_id ?? '', state.open && tab === 'positions', state.connectionReady)
  const assets = holdings.snapshot?.assets
  const positionNotice = [
    holdings.reconcileError != null ? '本次核对未完成。' : '',
    holdings.updateDelayed ? '持仓更新暂不可用。' : '',
    holdings.snapshot ? '当前显示上次读取结果。' : '尚未取得持仓快照，请稍后重试。',
  ].join('')
  const positionUnavailable = holdings.readUnavailable || holdings.reconcileError != null
  const observed = !!assets && Array.isArray(assets.positions) && !['assumed', 'error', 'unavailable'].includes(String(assets.source))
  const allPositions = observed ? assets.positions ?? [] : []
  const positions = allPositions.filter(position => (!symbol || position.symbol === symbol) && position.volume !== 0)
  const data = query.data
  const recordFailure = reconcileFailure?.key === `${key}:${limit}` ? reconcileFailure.message : null
  const recordsUnavailable = !state.connectionReady || query.error != null || !!data?.errors.length
  const recordsDelayed = useReadWarningDelay(`${key}:${limit}:${tab}`, recordsUnavailable, state.open && tab !== 'positions' && tab !== 'pending')
  const recordReasons = [...new Set([recordFailure, query.error?.message, ...(data?.errors ?? [])].filter(Boolean))].join('；')
  const recordNotice = `${recordFailure ? '本次核对未完成。' : '记录更新暂不可用。'}${data ? '当前显示上次读取结果。' : '尚未取得记录，请稍后重试。'}${recordReasons ? `原因：${recordReasons}` : ''}`
  const visibleCount = !data ? 0 : tab === 'operations' ? data.failures.length : tab === 'failures' ? endedActivityOrders(data).length : tab === 'trades' ? data.trades.length : data.orders.length
  const truncated = !!data?.truncated && visibleCount >= data.limit
  const positionKey = activityPositionKey(tab, symbol)
  const ready = tab === 'positions' ? holdings.query.data !== null && !holdings.query.loading : tab === 'pending' ? state.query.data !== null && !state.query.loading : data !== null && !query.loading
  const savedPosition = view.positions.get(positionKey)
  const reading = tab === 'positions' ? holdings.query.loading : tab === 'pending' ? state.query.loading : data === null && (query.loading || query.refreshing)
  const restoring = state.open && reading && !!(savedPosition?.top || savedPosition?.left)
  const position = useActivityPosition(positionKey, view.positions, ready, state.open)
  const onTabChange = (tab: ActivityTab) => { position.capture(); updateView({ tab }) }
  const chooseSymbol = (symbol: string) => { position.capture(); updateView(tab === 'positions' ? { positionSymbol: symbol } : tab === 'operations' ? { operationSymbol: symbol, operationLimit: 100 } : { symbol, limit: 100 }); setManual(false) }
  const symbols = [...new Set([...(tab === 'positions' ? allPositions.flatMap(position => position.symbol ? [position.symbol] : []) : data?.symbols ?? []), ...(symbol ? [symbol] : [])])].sort()
  const loading = tab === 'positions' ? holdings.query.loading : query.loading
  const refreshing = tab === 'positions' ? holdings.refreshing : query.refreshing
  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="mb-4 shrink-0 overflow-x-auto"><Segmented size="sm" className="whitespace-nowrap" value={tab} onChange={onTabChange} options={[
      { value: 'positions', label: '持仓' },
      { value: 'trades', label: '最近成交' }, { value: 'orders', label: '最近委托' },
      { value: 'pending', label: '在途委托', badge: state.query.data?.complete ? <span className="num text-ink-3">{state.query.data.count}</span> : undefined },
      { value: 'failures', label: '撤单 / 拒绝' }, { value: 'operations', label: '操作异常' },
    ]} /></div>
    <div className="flex min-h-0 flex-1 flex-col">
      <div hidden={tab === 'pending'} className={`mb-3 ${tab === 'pending' ? 'hidden' : 'flex'} shrink-0 flex-wrap items-center justify-between gap-3 text-[13px]`}>
        <div className="min-w-0 max-w-full">
          <div className="flex items-center gap-2">
            <span className="text-ink-3">品种</span>
            <Select value={symbol} ariaLabel="品种" searchable className="min-h-8 w-48 max-w-full justify-between px-2.5 py-1.5 text-[13px]" options={[
              { value: '', label: '全部品种' },
              ...symbols.map(value => ({ value, label: value })),
              ...(tab === 'positions' ? [] : [{ value: '__custom__', label: '输入其他品种…' }]),
            ]} onChange={value => { if (value === '__custom__') setManual(true); else chooseSymbol(value) }} />
            <Tooltip content={tab === 'positions' ? '账户最新持仓快照，包含此前开仓后仍持有的仓位；缺失字段显示 —。' : data?.scope || (query.loading ? '正在读取记录范围…' : '记录范围暂不可用')}>
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
        <Tooltip content={tab === 'positions' ? holdings.snapshot ? `持仓观测：${time(typeof assets?.update_time === 'string' ? assets.update_time : holdings.snapshot.created_at)}` : '尚未取得持仓快照' : data?.queried_at ? `上次核对：${time(data.queried_at)}` : '尚未核对渠道记录'}>
        <button type="button" aria-label={tab === 'positions' ? '核对持仓' : '核对记录'} disabled={!state.connectionReady || loading || refreshing} onClick={() => { if (tab === 'positions') void holdings.reconcile(); else { setReconcileFailure(null); request.current = { key, refresh: true, manual: true }; void query.refresh() } }} className="flex min-h-8 min-w-20 cursor-pointer items-center justify-end gap-1.5 text-accent disabled:cursor-default disabled:text-ink-3"><RefreshCw size={14} aria-hidden />{refreshing || loading ? '核对中' : tab === 'positions' ? '核对持仓' : '核对记录'}</button>
        </Tooltip>
      </div>
      {tab !== 'positions' && tab !== 'pending' && <p className="mb-3 shrink-0 text-xs text-ink-3">记录观测：{data ? time(data.sampled_at) : '尚未读取'}
        {(recordsDelayed || recordFailure) && <ReadStatusDot label={recordFailure ? '本次核对未完成' : '记录更新暂不可用'}>{recordNotice}</ReadStatusDot>}
      </p>}
      <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={position.ref} onScrollCapture={position.onScrollCapture} inert={restoring} aria-hidden={restoring || undefined} className={`min-h-0 flex-1 overflow-x-hidden overflow-y-auto [scrollbar-gutter:stable] ${restoring ? 'invisible' : ''}`} data-activity-scroll>
      <ContentSwap identity={tab} animateHeight={false}>
        {tab === 'positions' ? <>
          <p className="mb-3 text-xs text-ink-3">持仓观测：{holdings.snapshot ? time(typeof assets?.update_time === 'string' ? assets.update_time : holdings.snapshot.created_at) : '尚未查询'}
            {(holdings.updateDelayed || holdings.reconcileError != null) && <ReadStatusDot label={holdings.reconcileError != null ? '本次核对未完成' : '持仓更新暂不可用'}>{positionNotice}</ReadStatusDot>}
          </p>
          <PositionRecordsTable positions={positions} channel={channel} units={units} currency={assets?.currency} loading={holdings.query.loading}
            emptyText={!observed ? positionUnavailable ? '持仓暂不可用，请稍后重试' : '持仓尚未查询' : positionUnavailable ? '上次快照无对应持仓，当前持仓待核对' : symbol ? '该品种当前无持仓' : '当前空仓'} />
        </> : tab === 'pending' ? pendingContent : <>
          <ActivityTable data={data} tab={tab} units={units} channel={channel} loading={query.loading}
            emptyText={recordsUnavailable || recordFailure ? data ? '上次读取未发现对应记录，当前记录待核对' : '记录暂不可用，请稍后重试' : undefined} />
          {truncated && <div className="my-3 flex items-center gap-4 text-xs"><span className="text-ink-3">显示最近 {data?.limit} 笔记录</span>
          {limit < 500 && <button type="button" disabled={query.refreshing} onClick={() => updateView(tab === 'operations' ? { operationLimit: Math.min(500, limit + 100) } : { limit: Math.min(500, limit + 100) })} className="min-h-8 cursor-pointer text-[13px] text-accent disabled:text-ink-3">加载更多</button>}
          </div>}
        </>}
      </ContentSwap>
      </div>
      {restoring && <div role="status" className="pointer-events-none absolute inset-0 flex items-center justify-center text-xs text-ink-3">正在恢复记录位置…</div>}
      </div>
    </div>
  </div>
}
