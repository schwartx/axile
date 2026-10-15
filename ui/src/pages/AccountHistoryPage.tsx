import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router'
import { CircleHelp, RefreshCw, Save } from 'lucide-react'
import { useNavigate } from '@/components/ui/nav'
import { SectionLabel } from '@/components/ui/Card'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { PerformanceChart } from '@/components/viz/PerformanceChart'
import { PerformanceChartPlaceholder } from '@/features/history/PerformanceChartPlaceholder'
import { AccountPageTitle } from '@/features/account/pageHead'
import { FeeControl } from '@/features/history/FeeControl'
import { getAccount } from '@/lib/api/accounts'
import { getPortfolios } from '@/lib/api/portfolios'
import { shanghaiDay, shanghaiLabel } from '@/features/history/costs'
import { reconcileSelection, type ChartSelection } from '@/features/history/chartModel'
import { loadPerformanceView, performanceViews, savePerformanceView, type BacktestMode } from '@/features/history/viewState'
import { getPerformanceCosts, selectionQuery, savePerformanceSettings } from '@/lib/api/performance'
import { usePerformanceSnapshot } from '@/features/history/usePerformanceSnapshot'
import { snapshotPending } from '@/features/history/performanceCache'
import { usePolling } from '@/lib/hooks/usePolling'
import { withViewTransition } from '@/lib/viewTransition'
import { useDomainStore } from '@/stores/domain'
import { type RangeKey } from '@/features/history/derive'
import { settingsFromDraft } from '@/features/history/performance'
import type { PerformanceSettings } from '@/types/api'
import type { TimeScaleMode } from '@/features/history/timeScale'

const RANGES: Array<{ value: RangeKey; label: string }> = [
  { value: '30', label: '30 天' }, { value: '90', label: '90 天' }, { value: 'all', label: '全部' },
]
const VIEWS: Array<{ value: 'cumulative' | 'daily'; label: string }> = [
  { value: 'cumulative', label: '累计' }, { value: 'daily', label: '每日' },
]
const BACKTEST_MODES: Array<{ value: BacktestMode; label: string }> = [
  { value: 'sized', label: '数量换算后' }, { value: 'target', label: '目标权重' },
]
const SCALES: Array<{ value: TimeScaleMode; label: string }> = [
  { value: 'observations', label: '观测序列' }, { value: 'natural', label: '自然时间' },
]
const MARKERS: Array<{ value: 'on' | 'off'; label: string }> = [
  { value: 'on', label: '显示成交点' }, { value: 'off', label: '隐藏成交点' },
]

export function AccountHistoryPage() {
  const { id } = useParams()
  return <AccountHistory key={id} accountId={Number(id)} />
}

function AccountHistory({ accountId }: { accountId: number }) {
  const navigate = useNavigate()
  const item = useDomainStore(s => s.accounts?.find(account => account.account_id === accountId))
  const [range, setRange] = useState<RangeKey>(() => performanceViews.get(accountId)?.range ?? 'all')
  const [view, setView] = useState<'cumulative' | 'daily'>(() => performanceViews.get(accountId)?.view ?? 'cumulative')
  const [scale, setScale] = useState<TimeScaleMode>(() => performanceViews.get(accountId)?.scale ?? loadPerformanceView(accountId).scale ?? 'observations')
  const [selection, setSelection] = useState<ChartSelection>(() => performanceViews.get(accountId)?.selection ?? null)
  const [markers, setMarkers] = useState(() => performanceViews.get(accountId)?.markers ?? loadPerformanceView(accountId).markers ?? true)
  const [backtestMode, setBacktestMode] = useState<BacktestMode>(() => performanceViews.get(accountId)?.backtestMode ?? loadPerformanceView(accountId).backtestMode ?? 'sized')
  useEffect(() => {
    performanceViews.set(accountId, { ...performanceViews.get(accountId), range, view, scale, selection, markers, backtestMode })
    savePerformanceView(accountId, { scale, markers, backtestMode })
  }, [accountId, range, view, scale, selection, markers, backtestMode])
  const [showEvents, setShowEvents] = useState(false)
  const [draft, setDraft] = useState<{ mode: 'ts' | 'cs'; fee: string } | null>(null)
  const [saved, setSaved] = useState<PerformanceSettings | null>(null)
  const [saving, setSaving] = useState(false)
  const [editingFee, setEditingFee] = useState(false)
  const [saveError, setSaveError] = useState<Error | null>(null)
  const mounted = useRef(true)
  const saveLock = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const account = usePolling(useCallback((s: AbortSignal) => getAccount(accountId, s), [accountId]), { queryKey: `account:${accountId}`, intervalMs: 0 })
  const performance = usePerformanceSnapshot(accountId, range)
  const refreshPerformance = useRef(performance.refresh)
  useEffect(() => { refreshPerformance.current = performance.refresh }, [performance.refresh])
  const snapshot = performance.data
  const data = snapshot?.result
  const restoreScroll = useRef(performanceViews.get(accountId)?.scroll ?? null)
  const [selectionNotice, setSelectionNotice] = useState(false)
  useEffect(() => {
    const main = document.querySelector('main.app-workspace')
    const save = () => {
      const state = performanceViews.get(accountId)
      if (state) performanceViews.set(accountId, { ...state, scroll: main?.scrollTop ?? 0 })
    }
    main?.addEventListener('scroll', save, { passive: true })
    return () => { main?.removeEventListener('scroll', save) }
  }, [accountId])
  useLayoutEffect(() => {
    if (!data || restoreScroll.current == null) return
    const scroll = restoreScroll.current
    const frame = requestAnimationFrame(() => {
      const main = document.querySelector('main.app-workspace')
      if (main) main.scrollTop = scroll
      restoreScroll.current = null
    })
    return () => cancelAnimationFrame(frame)
  }, [data])
  useEffect(() => {
    if (!data) return
    setSelection(current => {
      const next = current?.kind === 'execution' && !data.executions?.some(row => row.record.id === current.recordId) ? null : reconcileSelection(current, data.points)
      if (current && !next) setSelectionNotice(true)
      return next
    })
  }, [data])
  const selectionKey = JSON.stringify(selectionQuery(selection))
  const intervalCosts = usePolling(useCallback((s: AbortSignal) => getPerformanceCosts(accountId, { snapshot_id: snapshot!.snapshot_id!, range, dimension: 'summary', ...JSON.parse(selectionKey) }, s), [accountId, snapshot?.snapshot_id, range, selectionKey]), {
    queryKey: `performance-summary:${accountId}:${snapshot?.snapshot_id}:${range}:${selectionKey}`, intervalMs: 0, enabled: !!snapshot?.snapshot_id && selection?.kind === 'interval',
  })
  const portfolios = usePolling(useCallback((s: AbortSignal) => getPortfolios(s), []), { queryKey: 'performance-portfolios', intervalMs: 0 })
  const portfolioNames = useMemo(() => new Map(portfolios.data?.data.flatMap(p => p.id == null ? [] : [[p.id, p.name] as const]) ?? []), [portfolios.data])
  const settings = saved ?? account.data ?? data?.settings
  const mode = 'cs'
  const fee = draft?.fee ?? String(Number(((settings?.backtest_fee_rate ?? 0) * 10000).toFixed(8)))
  const parsed = settingsFromDraft(mode, fee)
  const dirty = parsed != null && parsed.backtest_fee_rate !== settings?.backtest_fee_rate

  const save = async () => {
    if (!parsed || saveLock.current) return
    saveLock.current = true
    setSaving(true)
    setSaveError(null)
    try {
      const next = await savePerformanceSettings(accountId, parsed)
      if (!mounted.current) return
      setSaved(next)
      setDraft(null)
      void account.refresh()
      void refreshPerformance.current()
    } catch (error) {
      if (mounted.current) setSaveError(error instanceof Error ? error : new Error(String(error)))
    } finally {
      saveLock.current = false
      if (mounted.current) setSaving(false)
    }
  }

  const backtestBusy = snapshotPending(snapshot) || performance.checking
  const active = data?.points[data.points.length - 1]
  const daily = view === 'daily'
  const accountReturn = active?.account_return
  const portfolioReturn = backtestMode === 'target' ? active?.target_portfolio_return : active?.portfolio_return
  const difference = accountReturn != null && portfolioReturn != null ? portfolioReturn - accountReturn : null
  const costs = useMemo(() => snapshot?.result ? new Map(Object.entries(snapshot.daily_costs)) : null, [snapshot?.result, snapshot?.daily_costs])
  const events = snapshot?.events ?? []
  const calculationError = snapshot?.error ? new Error(snapshot.error) : performance.error
  const pendingSettings = !!data && settings?.backtest_fee_rate !== data.settings.backtest_fee_rate

  const controls = <div data-testid="performance-controls" className="flex min-w-0 flex-wrap items-center gap-2 sm:ml-auto sm:justify-end">
        <fieldset disabled={saving || !settings} className="flex min-w-0 flex-wrap items-center gap-2 disabled:opacity-60">
          <FeeControl fee={fee} onChange={value => setDraft({ mode, fee: value })} onEditingChange={setEditingFee} />
        </fieldset>
        <div className="flex items-center gap-2">
          <button type="button" aria-label={saving ? '保存中' : '保存并计算'} title={dirty ? '参数已修改，保存并计算' : '保存并计算'} disabled={!dirty || saving || !settings} onClick={() => void save()} className="flex h-9 w-9 items-center justify-center rounded border border-line text-sm disabled:opacity-40"><Save size={14} /></button>
          <button type="button" aria-label="刷新绩效与成本" title="刷新绩效与成本" disabled={saving || backtestBusy} onClick={() => void performance.refresh()} className="flex h-9 w-9 items-center justify-center rounded border border-line disabled:opacity-40"><RefreshCw size={14} className={backtestBusy ? 'animate-spin motion-reduce:animate-none' : ''} /></button>
          <span role="status" className="sr-only">{dirty && !saving ? '参数已修改，待计算' : ''}</span>
        </div>
        <Segmented size="sm" value={view} options={VIEWS} onChange={value => withViewTransition(() => setView(value))} />
        <fieldset><legend className="sr-only">回测口径</legend><Segmented size="sm" value={backtestMode} options={BACKTEST_MODES} onChange={setBacktestMode} trailing={
          <Tooltip content={<div className="space-y-1"><p><b>数量换算后：</b>按数量换算目标敞口，并非实际成交持仓。</p><p><b>目标权重：</b>直接按目标权重计算回测收益。</p></div>} arrow>
            <button type="button" aria-label="回测口径说明" className="flex h-9 w-8 items-center justify-center rounded text-ink-3 hover:text-ink-1 focus-visible:outline-accent"><CircleHelp size={15} aria-hidden="true" /></button>
          </Tooltip>
        } /></fieldset>
        <fieldset><legend className="sr-only">横轴尺度</legend><Segmented size="sm" value={scale} options={SCALES} onChange={setScale} /></fieldset>
        <fieldset><legend className="sr-only">成交点</legend><Segmented size="sm" value={markers ? 'on' : 'off'} options={MARKERS} onChange={value => setMarkers(value === 'on')} /></fieldset>
        </div>

  return <section className="min-w-0 [&_button]:min-h-9">
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        <AccountPageTitle
          accountId={accountId}
          page="实盘绩效"
          name={account.data?.name ?? item?.name}
          channel={account.data?.trade_channel ?? item?.trade_channel}
          market={account.data?.market ?? item?.market}
        />
        {data ? <dl className="flex min-w-0 flex-wrap items-baseline text-xs text-ink-3">
          <div className="whitespace-nowrap"><dt className="sr-only">基准</dt><dd className="inline">{data.baseline ? <time dateTime={data.baseline} title={shanghaiLabel(data.baseline)}>{shanghaiDay(data.baseline)}</time> : '—'}</dd><span aria-hidden="true">&nbsp;→&nbsp;</span><dt className="sr-only">截止</dt><dd className="inline">{data.end ? <time dateTime={data.end} title={shanghaiLabel(data.end)}>{shanghaiDay(data.end)}</time> : '—'}</dd><span aria-hidden="true">&nbsp;·</span></div>
          <div className="whitespace-nowrap"><dt className="sr-only">有效回测记录</dt><dd className="inline">回测 {data.backtest_included ? `${backtestMode === 'target' ? data.target_used_record_count ?? 0 : data.used_record_count}/${data.observation_count}` : 0}</dd><span aria-hidden="true">&nbsp;·</span></div>
          <div className="whitespace-nowrap"><dt className="sr-only">历史记录</dt><dd className="inline">历史 {data.record_count}</dd></div>
        </dl> : <span className="shrink-0 text-xs text-ink-3">{snapshot?.data_until ? `数据截止 ${shanghaiLabel(snapshot.data_until)}` : '暂无绩效快照'}</span>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Segmented size="sm" value={range} options={RANGES} onChange={value => withViewTransition(() => { setSelection(null); setRange(value) })} />
      </div>
    </div>
    <div className="border-t border-line pt-2">
      {difference != null && difference < 0 && <p className="mt-1 text-xs text-warn">账户收益高于回测，待核对差异</p>}
      {data && data.observation_count > 0 && (data.calendar.status === 'partial' || data.calendar.status === 'unavailable') && <p role="status" className="mt-1 text-xs text-warn">部分日期交易日历不可用，休市标记可能不完整</p>}
      {!parsed && !editingFee && <p role="alert" className="mt-2 text-xs text-warn">费率须大于等于 0 且小于 10000 BP</p>}
      <ErrorNotice intent="mutation" title="参数保存失败" error={saveError} />
      <ErrorNotice intent="read" title="账户设置读取失败" error={account.error} hasData={account.data != null} onRetry={account.refresh} />
    </div>
    {pendingSettings && <div role="status" className="flex flex-wrap items-center gap-x-4 gap-y-1 py-1 text-xs text-ink-3" data-testid="performance-status">
      {pendingSettings && <span className="text-warn">新费率 {Number(((settings?.backtest_fee_rate ?? 0) * 10000).toFixed(8))} BP 待计算</span>}
    </div>}
    <ErrorNotice intent="read" title={data ? '更新失败，保留上次结果' : '绩效读取失败'} error={calculationError} hasData={data != null} variant="compact" onRetry={performance.refresh} />
    {selectionNotice && <p role="status" className="py-1 text-xs text-ink-3">数据已更新，原选区已失效，请重新选择。</p>}
    <ErrorNotice intent="read" title="区间成本读取失败" error={intervalCosts.error} hasData={intervalCosts.data != null} variant="compact" onRetry={intervalCosts.refresh} />
    {!data && <PerformanceChartPlaceholder accountId={accountId} controls={controls} loading={backtestBusy || !snapshot && !calculationError} failed={!!calculationError} />}
    {data && <div className="pb-4">
      <PerformanceChart key={range} accountId={accountId} snapshotId={snapshot?.snapshot_id} viewKey={`${accountId}:${range}`} data={data} daily={daily} backtestMode={backtestMode} scaleMode={scale} costs={costs} intervalCost={intervalCosts.error || intervalCosts.loading ? null : intervalCosts.data?.summary ?? null} onSelect={value => { setSelectionNotice(false); setSelection(value) }} selection={selection} portfolioNames={portfolioNames} controls={controls} showExecutions={markers} />
      {backtestMode === 'sized' && !!data.sizing_fallback_count && <p role="status" className="mt-2 text-xs text-ink-3">{data.sizing_fallback_count} 条参与回测的执行记录因缺少数量换算证据，按目标权重计算：{data.sizing_fallback_ranges?.map(range => `${shanghaiLabel(range.start)} → ${shanghaiLabel(range.end)}（${range.count} 条）`).join('、')}</p>}
      {(backtestMode === 'target' ? data.target_skips : data.skips)?.count ? <p role="status" className="mt-2 text-xs text-warn">{(backtestMode === 'target' ? data.target_skips : data.skips)?.count} 条执行快照未参与回测（{[(backtestMode === 'target' ? data.target_skips : data.skips)?.missing_target ? `目标权重缺失 ${(backtestMode === 'target' ? data.target_skips : data.skips)?.missing_target} 条` : null, (backtestMode === 'target' ? data.target_skips : data.skips)?.missing_ticks ? `缺少首笔盘口 ${(backtestMode === 'target' ? data.target_skips : data.skips)?.missing_ticks} 条` : null].filter(Boolean).join('、')}），组合收益按最后持仓延续估算</p> : null}
      {data.invalid_asset_count > 0 && <p className="mt-2 text-xs text-warn">{data.invalid_asset_count} 条账户资产快照不可用</p>}
    </div>}
    <div className="border-t border-line py-4">
      <SectionLabel>账户时间线</SectionLabel>
      {events.length === 0 ? <p className="text-sm text-ink-3">本区间无异常事件</p> : events.slice(0, showEvents ? undefined : 8).map((event, i) => <div key={i} className="flex flex-wrap items-baseline gap-3 border-b border-line py-2 text-xs">
        <span className="text-ink-3">{shanghaiLabel(event.time)}</span><span className={event.tag === '失败' ? 'text-warn' : 'text-ink-2'}>{event.tag}</span>
        {event.executionId ? <button className="min-h-9 break-all text-left hover:underline" onClick={() => navigate(`/accounts/${accountId}/executions/${event.executionId}`)}>{event.text}</button> : <span>{event.text}</span>}
      </div>)}
      {events.length > 8 && <button className="min-h-9 text-xs text-accent" onClick={() => withViewTransition(() => setShowEvents(!showEvents))}>{showEvents ? '收起' : `展开全部 ${events.length} 条`}</button>}
      {(snapshot?.event_count ?? 0) > events.length && <p className="text-xs text-ink-3">共 {snapshot?.event_count} 条事件，显示最近 {events.length} 条</p>}
    </div>
  </section>
}
