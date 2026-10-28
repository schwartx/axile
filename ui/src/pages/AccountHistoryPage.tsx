import { ContributionPanel } from '@/features/history/ContributionPanel'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router'
import { CircleHelp, Info, RefreshCw, Save, X } from 'lucide-react'
import { useNavigate } from '@/components/ui/nav'
import { SectionLabel } from '@/components/ui/Card'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
import { Select } from '@/components/ui/Select'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { PerformanceChart } from '@/components/viz/PerformanceChart'
import { PerformanceChartPlaceholder } from '@/features/history/PerformanceChartPlaceholder'
import { AccountPageTitle } from '@/features/account/pageHead'
import { FeeControl } from '@/features/history/FeeControl'
import { getAccount } from '@/lib/api/accounts'
import { getPortfolios } from '@/lib/api/portfolios'
import { shanghaiLabel } from '@/features/history/costs'
import { intervalSelection, pointTime, reconcileSelection, type ChartSelection } from '@/features/history/chartModel'
import { loadPerformanceView, performanceViews, savePerformanceView, type BacktestMode } from '@/features/history/viewState'
import { getPerformanceCosts, selectionQuery, savePerformanceSettings, type AnalysisMetadata } from '@/lib/api/performance'
import { usePerformanceSnapshot } from '@/features/history/usePerformanceSnapshot'
import { ApiError } from '@/lib/api/client'
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

/** Distinguish a ready publication from a chart that has finished loading. */
function historyLoadingMessage(metadata: AnalysisMetadata | null, hasResult: boolean, loadingResult: boolean, unavailable: boolean): string | undefined {
  if (unavailable) return metadata?.status === 'pending' || metadata?.status === 'stale' ? '正在重新计算，完成后显示绩效结果' : '当前快照需要重新计算，请刷新绩效与成本'
  if (loadingResult) return hasResult ? '正在读取新版绩效，当前保留上次结果' : '正在读取绩效结果'
  if (metadata?.status === 'pending' || metadata?.status === 'stale') return hasResult ? '正在重新计算，当前保留上次结果' : '正在计算绩效'
  if (metadata?.status === 'failed' && metadata.retry_at != null) return hasResult ? '计算失败，等待重试；当前保留上次结果' : '计算失败，等待重试'
  return undefined
}

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
  const metadata = performance.metadata
  const snapshot = performance.displayed
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
    if (!snapshot) return
    setSelection(current => {
      const next = reconcileSelection(current, snapshot.result.points, snapshot.execution_index)
      if (current && !next) setSelectionNotice(true)
      return next
    })
  }, [snapshot])
  const selectionKey = JSON.stringify(selectionQuery(selection))
  const displayedRange = snapshot?.range ?? range
  const intervalCosts = usePolling(useCallback((s: AbortSignal) => getPerformanceCosts(accountId, { snapshot_id: snapshot!.snapshot_id, range: displayedRange, dimension: 'summary', ...JSON.parse(selectionKey) }, s), [accountId, snapshot?.snapshot_id, displayedRange, selectionKey]), {
    queryKey: `performance-summary:${accountId}:${snapshot?.snapshot_id}:${displayedRange}:${selectionKey}`, intervalMs: 0, enabled: !!snapshot?.snapshot_id && selection?.kind === 'interval',
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

  const computing = metadata?.status === 'pending' || metadata?.status === 'stale' || metadata?.status === 'failed' && metadata.retry_at != null
  const backtestBusy = computing || performance.checking || performance.loadingResult
  const active = data?.points[data.points.length - 1]
  const daily = view === 'daily'
  const accountReturn = active?.account_return
  const portfolioReturn = backtestMode === 'target' ? active?.target_portfolio_return : active?.portfolio_return
  const difference = accountReturn != null && portfolioReturn != null ? portfolioReturn - accountReturn : null
  const costs = useMemo(() => snapshot?.result ? new Map(Object.entries(snapshot.daily_costs)) : null, [snapshot?.result, snapshot?.daily_costs])
  const events = snapshot?.events ?? []
  const calculationError = useMemo(() => metadata?.error ? new Error(metadata.error) : null, [metadata?.error])
  const readViewUnavailable = !!metadata?.snapshot_id && metadata.read_view_schema_version < 1
    || performance.resultError instanceof ApiError && performance.resultError.code === 'ANALYSIS_READ_VIEW_UNAVAILABLE'
  const loadingMessage = historyLoadingMessage(metadata, !!data, performance.loadingResult, readViewUnavailable)
  const initialError = calculationError || performance.statusError || performance.resultError || performance.refreshError
  const pendingSettings = !!data && settings?.backtest_fee_rate !== data.settings.backtest_fee_rate
  const intervalOptions = useMemo(() => data?.points.map(point => ({ value: pointTime(point), label: point.observed_at ? shanghaiLabel(point.observed_at) : point.date })) ?? [], [data?.points])
  const intervalStart = selection?.kind === 'interval' ? selection.start : intervalOptions[0]?.value
  const intervalEnd = selection?.kind === 'interval' ? selection.end : intervalOptions.at(-1)?.value

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
        <fieldset><legend className="sr-only">成交点</legend><Segmented size="sm" value={markers ? 'on' : 'off'} options={MARKERS} onChange={value => setMarkers(value === 'on')} /></fieldset>
        </div>

  return <section className="min-w-0 [&_button]:min-h-9">
    <div data-testid="performance-sticky-header" className="sticky top-0 z-30 flex flex-wrap items-center gap-x-1 gap-y-1 border-b border-line bg-bg py-1">
    <div className="flex shrink-0 items-center [&_h1]:text-sm!">
      <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1 [&>div]:gap-1!">
        <AccountPageTitle
          accountId={accountId}
          page="实盘绩效"
          name={account.data?.name ?? item?.name}
          channel={account.data?.trade_channel ?? item?.trade_channel}
          market={account.data?.market ?? item?.market}
        />
        <Tooltip content={data ? <dl className="space-y-1 text-xs">
          <div><dt className="inline text-ink-3">数据覆盖：</dt><dd className="inline">{data.baseline ? shanghaiLabel(data.baseline) : '—'} → {data.end ? shanghaiLabel(data.end) : '—'}</dd></div>
          <div><dt className="inline text-ink-3">有效回测记录：</dt><dd className="inline">{data.backtest_included ? `${backtestMode === 'target' ? data.target_used_record_count ?? 0 : data.used_record_count}/${data.observation_count}` : 0}</dd></div>
          <div><dt className="inline text-ink-3">历史记录：</dt><dd className="inline">{data.record_count}</dd></div>
        </dl> : <span>{metadata?.data_until ? `数据截止 ${shanghaiLabel(metadata.data_until)}` : '暂无绩效快照'}</span>} arrow>
          <button type="button" aria-label="绩效数据说明" className="inline-flex h-[30px] w-6 min-h-0! shrink-0 items-center justify-center rounded text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent"><Info size={15} aria-hidden="true" /></button>
        </Tooltip>
      </div>
    </div>
    <div role="group" aria-label="绩效与品种贡献共用筛选" data-testid="performance-shared-controls" className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1 text-xs text-ink-2 [&_button]:min-h-0!">
      <fieldset><legend className="sr-only">绩效时间范围</legend><Segmented size="sm" className="rounded-md! [&_button]:h-[26px] [&_button]:px-2 [&_button]:py-0" value={range} options={RANGES} onChange={value => withViewTransition(() => { setSelectionNotice(false); setSelection(null); setRange(value) })} /></fieldset>
      <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1" role="group" aria-label="分析区间">
        {(['start', 'end'] as const).map(edge => <label key={edge} className="flex min-w-0 items-center gap-1">
          <span className="shrink-0 text-ink-3">{edge === 'start' ? '起点' : '终点'}</span>
          <Select<number> ariaLabel={edge === 'start' ? '比较起点' : '比较终点'} appearance="inline" className="h-[30px] w-auto min-w-0 gap-1! px-1 text-xs"
            disabled={intervalOptions.length < 2} value={(edge === 'start' ? intervalStart : intervalEnd) ?? 0}
            options={intervalOptions.filter(option => option.value !== (edge === 'start' ? intervalEnd : intervalStart))}
            placeholder="暂无观测"
            selectedValue={(() => {
              const option = intervalOptions.find(option => option.value === (edge === 'start' ? intervalStart : intervalEnd))
              return option ? <Tooltip content={option.label}><span>{option.label.slice(0, 16)}</span></Tooltip> : undefined
            })()}
            onChange={value => {
              const other = edge === 'start' ? intervalEnd : intervalStart
              if (other == null) return
              setSelectionNotice(false)
              setSelection(intervalSelection(value, other))
            }} />
        </label>)}
        <button type="button" aria-label="取消选择" disabled={!selection} onClick={() => { setSelectionNotice(false); setSelection(null) }} className="inline-flex h-[30px] w-6 shrink-0 items-center justify-center rounded hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-40"><X size={15} aria-hidden="true" /></button>
      </div>
      <fieldset><legend className="sr-only">横轴尺度</legend><Segmented size="sm" className="rounded-md! [&_button]:h-[26px] [&_button]:px-2 [&_button]:py-0" value={scale} options={SCALES} onChange={setScale} /></fieldset>
    </div>
    </div>
    <div className="pt-2">
      {difference != null && difference < 0 && <p className="mt-1 text-xs text-warn">账户收益高于回测，待核对差异</p>}
      {data && data.observation_count > 0 && (data.calendar.status === 'partial' || data.calendar.status === 'unavailable') && <p role="status" className="mt-1 text-xs text-warn">部分日期交易日历不可用，休市标记可能不完整</p>}
      {!parsed && !editingFee && <p role="alert" className="mt-2 text-xs text-warn">费率须大于等于 0 且小于 10000 BP</p>}
      <ErrorNotice intent="mutation" title="参数保存失败" error={saveError} />
      <ErrorNotice intent="read" title="账户设置读取失败" error={account.error} hasData={account.data != null} onRetry={account.refresh} />
    </div>
    {pendingSettings && <div role="status" className="flex flex-wrap items-center gap-x-4 gap-y-1 py-1 text-xs text-ink-3" data-testid="performance-status">
      {pendingSettings && <span className="text-warn">新费率 {Number(((settings?.backtest_fee_rate ?? 0) * 10000).toFixed(8))} BP 待计算</span>}
    </div>}
    <ErrorNotice intent="read" title="绩效状态读取失败" error={performance.statusError} hasData={metadata != null} variant="compact" onRetry={performance.check} />
    <ErrorNotice intent="read" title={data ? '新版绩效读取失败，保留上次结果' : '绩效结果读取失败'} error={performance.resultError} hasData={data != null} variant="compact" onRetry={performance.check} />
    <ErrorNotice intent="read" title={data ? '绩效计算失败，保留上次结果' : '绩效计算失败'} error={calculationError} hasData={data != null} variant="compact" onRetry={() => void performance.refresh()} />
    <ErrorNotice intent="mutation" title="刷新请求失败" error={performance.refreshError} variant="compact" onRetry={() => void performance.refresh()} />
    <div className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${loadingMessage ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden" inert={!loadingMessage} aria-hidden={!loadingMessage}>
        <p role="status" className="py-1 text-xs text-ink-3" data-testid="performance-loading-status">{loadingMessage}</p>
      </div>
    </div>
    {selectionNotice && <p role="status" className="py-1 text-xs text-ink-3">数据已更新，原选区已失效，请重新选择。</p>}
    <ErrorNotice intent="read" title="区间成本读取失败" error={intervalCosts.error} hasData={intervalCosts.data != null} variant="compact" onRetry={intervalCosts.refresh} />
    {!data && <PerformanceChartPlaceholder accountId={accountId} controls={controls} loading={backtestBusy || !metadata && !initialError} failed={!!initialError} message={loadingMessage} />}
    {data && <div className="pb-4">
      <PerformanceChart key={displayedRange} accountId={accountId} snapshotId={snapshot?.snapshot_id} viewKey={`${accountId}:${displayedRange}`} data={data} executionIndex={snapshot!.execution_index} daily={daily} backtestMode={backtestMode} scaleMode={scale} costs={costs} intervalCost={intervalCosts.error || intervalCosts.loading ? null : intervalCosts.data?.summary ?? null} onSelect={value => { setSelectionNotice(false); setSelection(value) }} selection={selection} portfolioNames={portfolioNames} controls={controls} showExecutions={markers} />

    </div>}
    {snapshot && <ContributionPanel key={accountId} chart={snapshot} selection={selection} scaleMode={scale} />}
    <div className="border-t border-line py-4">
      <SectionLabel>账户时间线</SectionLabel>
      {events.length === 0 ? <p className="text-sm text-ink-3">{snapshot ? '本区间无异常事件' : '绩效结果就绪后显示账户事件'}</p> : events.slice(0, showEvents ? undefined : 8).map((event, i) => <div key={i} className="flex flex-wrap items-baseline gap-3 border-b border-line py-2 text-xs">
        <span className="text-ink-3">{shanghaiLabel(event.time)}</span><span className={event.tag === '失败' ? 'text-warn' : 'text-ink-2'}>{event.tag}</span>
        {event.executionId ? <button className="min-h-9 break-all text-left hover:underline" onClick={() => navigate(`/accounts/${accountId}/executions/${event.executionId}`)}>{event.text}</button> : <span>{event.text}</span>}
      </div>)}
      {events.length > 8 && <button className="min-h-9 text-xs text-accent" onClick={() => withViewTransition(() => setShowEvents(!showEvents))}>{showEvents ? '收起' : `展开全部 ${events.length} 条`}</button>}
      {(snapshot?.event_count ?? 0) > events.length && <p className="text-xs text-ink-3">共 {snapshot?.event_count} 条事件，显示最近 {events.length} 条</p>}
    </div>
  </section>
}
