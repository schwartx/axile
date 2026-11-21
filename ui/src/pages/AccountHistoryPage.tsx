import { PerformanceSymbolSelect, SymbolSlippage } from '@/features/history/PerformanceSymbolSelect'
import { SymbolPerformance } from '@/features/history/SymbolPerformance'
import { loadAnalysisInputs, previewPerformance, type FeeResult, type CostView, type CostViewQuery } from '@/features/history/localAnalysis'
import { useLocalAnalysis } from '@/features/history/useLocalAnalysis'
import { ContributionPanel } from '@/features/history/ContributionPanel'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router'
import { CircleHelp, Info, RefreshCw, Save, X } from 'lucide-react'
import { useNavigate } from '@/components/ui/nav'
import { SectionLabel } from '@/components/ui/Card'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { PerformanceChart } from '@/components/viz/PerformanceChart'
import { PerformanceChartPlaceholder } from '@/features/history/PerformanceChartPlaceholder'
import { ObservationEndpointPicker } from '@/features/history/ObservationEndpointPicker'
import { AccountPageTitle } from '@/features/account/pageHead'
import { FeeControl } from '@/features/history/FeeControl'
import { getAccount } from '@/lib/api/accounts'
import { getPortfolios } from '@/lib/api/portfolios'
import { shanghaiLabel } from '@/features/history/costs'
import { intervalSelection, pointTime, reconcileSelection, type ChartSelection } from '@/features/history/chartModel'
import { loadPerformanceView, performanceViewports, performanceViews, savePerformanceView, type BacktestMode } from '@/features/history/viewState'
import { getPerformanceCosts, selectionQuery, savePerformanceSettings, type AnalysisMetadata } from '@/lib/api/performance'
import { usePerformanceSnapshot } from '@/features/history/usePerformanceSnapshot'
import { usePolling } from '@/lib/hooks/usePolling'
import { withViewTransition } from '@/lib/viewTransition'
import type { RangeTarget } from '@/features/history/rangeZoom'
import { useDomainStore } from '@/stores/domain'
import { type RangeKey } from '@/features/history/derive'
import { settingsFromDraft } from '@/features/history/performance'
import type { PerformanceSettings } from '@/types/api'
import type { TimeScaleMode } from '@/features/history/timeScale'
import { useWasmResource } from '@/lib/wasmResources'

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
function historyLoadingMessage(metadata: AnalysisMetadata | null, hasResult: boolean, loadingResult: boolean): string | undefined {
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
  const [selection, setSelection] = useState<ChartSelection>(() => {
    const saved = performanceViews.get(accountId)?.selection
    return saved?.kind === 'execution' ? null : saved ?? null
  })
  const [pickerSelection, setPickerSelection] = useState<ChartSelection>(null)
  const slideInterval = pickerSelection != null && pickerSelection === selection
  const [markers, setMarkers] = useState(() => performanceViews.get(accountId)?.markers ?? loadPerformanceView(accountId).markers ?? true)
  const [backtestMode, setBacktestMode] = useState<BacktestMode>(() => performanceViews.get(accountId)?.backtestMode ?? loadPerformanceView(accountId).backtestMode ?? 'sized')
  const [showEvents, setShowEvents] = useState(false)
  const [draft, setDraft] = useState<{ fee: string } | null>(null)
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
  const [lastSnapshot, setLastSnapshot] = useState(performance.displayed)
  const [selectionNotice, setSelectionNotice] = useState(false)
  const nextSnapshot = performance.displayed
  const rangeChanged = !!nextSnapshot && !!lastSnapshot && nextSnapshot.range !== lastSnapshot.range
  const pendingTarget = rangeChanged && nextSnapshot.range === range
    && lastSnapshot.result.observation_count >= 2 && lastSnapshot.result.points.length >= 2
    && !window.matchMedia('(prefers-reduced-motion: reduce)').matches ? nextSnapshot : null
  if (nextSnapshot && nextSnapshot !== lastSnapshot && !pendingTarget) {
    if (rangeChanged) {
      setSelection(null)
      setSelectionNotice(false)
    }
    setLastSnapshot(nextSnapshot)
  }
  const rangeTarget = useMemo<RangeTarget | undefined>(() => pendingTarget ? {
    chart: pendingTarget,
    finish: () => {
      performanceViewports.delete(`${accountId}:${pendingTarget.range}`)
      setSelection(null)
      setSelectionNotice(false)
      setLastSnapshot(pendingTarget)
    },
  } : undefined, [accountId, pendingTarget])
  const snapshot = lastSnapshot
  const displayedRange = snapshot?.range ?? range
  const data = snapshot?.result
  const restoreScroll = useRef(performanceViews.get(accountId)?.scroll ?? null)
  useEffect(() => {
    performanceViews.set(accountId, { ...performanceViews.get(accountId), range: displayedRange, view, scale, selection, markers, backtestMode })
    savePerformanceView(accountId, { scale, markers, backtestMode })
  }, [accountId, displayedRange, view, scale, selection, markers, backtestMode])
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
  const portfolios = usePolling(useCallback((s: AbortSignal) => getPortfolios(s, 'all'), []), { queryKey: 'performance-portfolios', intervalMs: 0 })
  const portfolioNames = useMemo(() => new Map(portfolios.data?.data.flatMap(p => p.id == null ? [] : [[p.id, p.name] as const]) ?? []), [portfolios.data])
  const settings = saved ?? account.data ?? data?.settings
  const fee = draft?.fee ?? String(Number(((settings?.backtest_fee_rate ?? 0) * 10000).toFixed(8)))
  const parsed = useMemo(() => settingsFromDraft(fee), [fee])
  const dirty = parsed != null && parsed.backtest_fee_rate !== settings?.backtest_fee_rate
  const [costSymbol, setCostSymbol] = useState('')
  const [costSide, setCostSide] = useState('')
  const [chartSymbol, setChartSymbol] = useState('')
  const requestedSymbol = useRef('')
  const symbolTransition = useRef<ViewTransition | undefined>(undefined)
  const chartKey = `${displayedRange}:${chartSymbol ? 'symbol' : 'account'}`
  const { resource: analysisWasm, state: analysisDownload } = useWasmResource('analysis')
  const analysisReady = analysisDownload.phase === 'cached'
  useEffect(() => {
    if (analysisDownload.phase === 'missing') void analysisWasm.download()
  }, [analysisDownload.phase, analysisWasm])
  const analysisError = useMemo(() => analysisDownload.error ? new Error(analysisDownload.error) : null, [analysisDownload.error])
  const inputs = usePolling(useCallback((signal: AbortSignal) => loadAnalysisInputs(accountId, snapshot!.version, displayedRange, 'costs', signal, JSON.parse(selectionKey)), [accountId, snapshot?.version, displayedRange, selectionKey]), {
    queryKey: `analysis-inputs:${accountId}:${snapshot?.version}:${displayedRange}:${selectionKey}`, intervalMs: 0, enabled: !!snapshot?.version && analysisReady, manual: true, retainDataWhenDisabled: true,
  })
  const refreshInputs = inputs.refresh
  useEffect(() => {
    if (analysisReady && !inputs.data && !inputs.error && !inputs.loading && !inputs.refreshing) void refreshInputs()
  }, [analysisReady, inputs.data, inputs.error, inputs.loading, inputs.refreshing, refreshInputs])
  const inputData = analysisReady && inputs.data?.version === snapshot?.version && inputs.data?.range === displayedRange ? inputs.data : null
  const localQuery = useMemo<CostViewQuery>(() => {
    const { day, start, end } = selectionQuery(selection)
    return { window: { day, start, end }, symbol: costSymbol, side: costSide }
  }, [selection, costSymbol, costSide])
  const local = useLocalAnalysis<typeof localQuery, CostView>(inputData, localQuery)
  const replaySnapshot = chartSymbol ? pendingTarget ?? snapshot : snapshot
  const replaySymbol = pendingTarget && chartSymbol ? chartSymbol : costSymbol
  const replayData = replaySnapshot?.result
  const needsReplay = dirty || (!!replaySymbol && !!replayData?.backtest_included)
  const feeInputs = usePolling(useCallback((signal: AbortSignal) => loadAnalysisInputs(accountId, replaySnapshot!.version, replaySnapshot!.range, 'fees', signal), [accountId, replaySnapshot?.version, replaySnapshot?.range]), {
    queryKey: `analysis-fees:${accountId}:${replaySnapshot?.version}:${replaySnapshot?.range}`, intervalMs: 0, enabled: !!replaySnapshot?.version && needsReplay && analysisReady, manual: true, retainDataWhenDisabled: true,
  })
  const refreshFeeInputs = feeInputs.refresh
  useEffect(() => {
    if (needsReplay && analysisReady && !feeInputs.data && !feeInputs.error && !feeInputs.loading && !feeInputs.refreshing) void refreshFeeInputs()
  }, [needsReplay, analysisReady, feeInputs.data, feeInputs.error, feeInputs.loading, feeInputs.refreshing, refreshFeeInputs])
  const feeData = analysisReady && feeInputs.data?.version === replaySnapshot?.version && feeInputs.data?.range === replaySnapshot?.range ? feeInputs.data : null
  const feeQuery = useMemo(() => dirty && parsed ? { fee_rate: parsed.backtest_fee_rate } : null, [dirty, parsed])
  const trial = useLocalAnalysis<typeof feeQuery, FeeResult>(feeQuery ? feeData : null, feeQuery)
  const contributionQuery = useMemo(() => replaySymbol && replayData?.backtest_included ? {
    contribution: [replaySymbol, backtestMode === 'target'] as [string, boolean],
    fee_rate: dirty && parsed ? parsed.backtest_fee_rate : replayData.settings.backtest_fee_rate,
  } : null, [replaySymbol, backtestMode, dirty, parsed, replayData])
  const replay = useLocalAnalysis<typeof contributionQuery, [number, number][]>(contributionQuery ? feeData : null, contributionQuery)
  const replayScope = `${snapshot?.version}:${displayedRange}:${backtestMode}:${dirty && parsed ? parsed.backtest_fee_rate : data?.settings.backtest_fee_rate}`
  const [displayedReplay, setDisplayedReplay] = useState<{ symbol: string; scope: string; samples: [number, number][] } | null>(null)
  if (replaySnapshot === snapshot && chartSymbol === costSymbol && replay.value && replay.value !== displayedReplay?.samples) {
    setDisplayedReplay({ symbol: chartSymbol, scope: replayScope, samples: replay.value })
  }
  const cachedSamples = displayedReplay?.symbol === chartSymbol && displayedReplay.scope === replayScope ? displayedReplay.samples : null
  const chartSamples = replaySnapshot === snapshot && chartSymbol === costSymbol ? replay.value ?? cachedSamples : cachedSamples
  const symbolReady = !contributionQuery || replay.value != null || !!(analysisError || feeInputs.error || replay.error)
  const symbolRange = rangeTarget && (replaySnapshot === snapshot || symbolReady) ? rangeTarget : undefined
  useEffect(() => {
    if (chartSymbol === costSymbol || !symbolReady || rangeTarget) return
    if (!costSymbol) {
      setChartSymbol('')
      return
    }
    let current = true
    symbolTransition.current = withViewTransition(() => {
      if (current && requestedSymbol.current === costSymbol) setChartSymbol(costSymbol)
    }, chartSymbol ? 'performance-symbol-slide' : 'performance-symbol')
    return () => { current = false }
  }, [chartSymbol, costSymbol, symbolReady, rangeTarget])
  const listedSymbols = useMemo(() => {
    const names = new Set(snapshot?.contributions?.map(row => row.symbol))
    for (const row of snapshot?.execution_index ?? []) {
      for (const symbol of row.trade_symbols) names.add(symbol)
    }
    return [...names].sort()
  }, [snapshot])
  const selectSymbol = (symbol: string, side = '') => {
    if (symbol !== requestedSymbol.current) symbolTransition.current?.skipTransition()
    requestedSymbol.current = symbol
    setCostSymbol(symbol)
    setCostSide(side)
  }
  const trialMessage = costSymbol && parsed ? `品种回测按 ${fee} BP 试算` : trial.value?.preview_error ?? (analysisError || feeInputs.error || trial.error ? '试算不可用，当前显示正式曲线' : trial.value?.preview ? '图中回测曲线为本地试算；账户收益仍为正式记录' : '正在准备完整窗口，当前仍显示正式曲线')
  const chartData = useMemo(() => snapshot && dirty && parsed && trial.value?.preview ? previewPerformance(snapshot, trial.value.preview, parsed.backtest_fee_rate) : data, [snapshot, dirty, parsed, trial.value?.preview, data])
  const intervalCosts = usePolling(useCallback((s: AbortSignal) => getPerformanceCosts(accountId, { version: snapshot!.version, range: displayedRange, dimension: 'summary', ...JSON.parse(selectionKey) }, s), [accountId, snapshot?.version, displayedRange, selectionKey]), {
    queryKey: `performance-summary:${accountId}:${snapshot?.version}:${displayedRange}:${selectionKey}`, intervalMs: 0, enabled: !!snapshot?.version && selection?.kind === 'interval' && (!analysisReady || !!inputs.error || !!local.error),
  })

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
  const daily = view === 'daily'
  const costs = useMemo(() => snapshot?.result ? new Map(Object.entries(snapshot.daily_costs)) : null, [snapshot?.result, snapshot?.daily_costs])
  const events = snapshot?.events ?? []
  const calculationError = useMemo(() => metadata?.error ? new Error(metadata.error) : null, [metadata?.error])
  const initialError = calculationError || performance.statusError || performance.resultError || performance.refreshError
  const showingPreviousRange = displayedRange !== range
  const waitingForRange = showingPreviousRange && !initialError
  const loadingMessage = (chartSymbol ? symbolRange : rangeTarget) ? '正在切换所选范围' : showingPreviousRange
    ? waitingForRange ? '正在读取所选范围，当前保留原图' : '当前仍显示上一范围的绩效'
    : historyLoadingMessage(metadata, !!data, performance.loadingResult)
  const backtestBusy = computing || performance.checking || performance.loadingResult || waitingForRange
  const pendingSettings = !!data && settings?.backtest_fee_rate !== data.settings.backtest_fee_rate
  const intervalTimes = useMemo(() => [...new Set(data?.points.filter(point => point.observed_at).map(pointTime).filter(Number.isFinite))], [data?.points])
  const intervalStart = selection?.kind === 'interval' ? selection.start : intervalTimes[0]
  const intervalEnd = selection?.kind === 'interval' ? selection.end : intervalTimes.at(-1)

  const controls = <div data-testid="performance-controls" className="flex w-full min-w-0 flex-wrap items-center gap-2 py-1">
        <fieldset disabled={saving || !settings || !!account.data?.archived_at} className="flex min-w-0 flex-wrap items-center gap-2 disabled:opacity-60">
          <FeeControl fee={fee} onChange={value => setDraft({ fee: value })} onEditingChange={setEditingFee} />
        </fieldset>
        <div className="flex items-center gap-2">
          <button type="button" aria-label={saving ? '保存中' : '保存并计算'} title={dirty ? '参数已修改，保存并计算' : '保存并计算'} disabled={!dirty || saving || !settings} onClick={() => void save()} className="flex h-9 w-9 items-center justify-center rounded border border-line text-sm disabled:opacity-40"><Save size={14} /></button>
          <button type="button" aria-label="刷新绩效与成本" title="刷新绩效与成本" disabled={saving || backtestBusy} onClick={() => void performance.refresh()} className="flex h-9 w-9 items-center justify-center rounded border border-line disabled:opacity-40"><RefreshCw size={14} className={backtestBusy ? 'animate-spin motion-reduce:animate-none' : ''} /></button>
          <span role="status" className="sr-only">{dirty && !saving ? '参数已修改，待计算' : ''}</span>
        </div>
        <fieldset><legend className="sr-only">回测口径</legend><Segmented size="sm" value={backtestMode} options={BACKTEST_MODES} onChange={setBacktestMode} trailing={
          <Tooltip content={<div className="space-y-1"><p><b>数量换算后：</b>按数量换算目标敞口，并非实际成交持仓。</p><p><b>目标权重：</b>直接按目标权重计算回测收益。</p></div>} arrow>
            <button type="button" aria-label="回测口径说明" className="flex h-9 w-8 items-center justify-center rounded text-ink-3 hover:text-ink-1 focus-visible:outline-accent"><CircleHelp size={15} aria-hidden="true" /></button>
          </Tooltip>
        } /></fieldset>
        <fieldset><legend className="sr-only">成交点</legend><Segmented size="sm" value={markers ? 'on' : 'off'} options={MARKERS} onChange={value => setMarkers(value === 'on')} /></fieldset>
        <div className={chartSymbol ? 'invisible' : undefined} inert={!!chartSymbol}>
          <Segmented size="sm" value={view} options={VIEWS} onChange={setView} />
        </div>
        </div>

  return <section className="min-w-0 [&_button]:min-h-9">
    <div data-testid="performance-sticky-header" className="sticky top-0 z-30 flex flex-wrap items-center gap-x-1 gap-y-1 border-b border-line bg-bg py-1">
    <div className="flex w-full min-w-0 items-center [&_h1]:text-sm!">
      <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1 [&>div]:gap-1!">
        <AccountPageTitle
          accountId={accountId}
          page="实盘绩效"
          name={account.data?.name ?? item?.name}
          channel={account.data?.trade_channel ?? item?.trade_channel}
          market={account.data?.market ?? item?.market}
        />
        {snapshot && <PerformanceSymbolSelect scope={`${snapshot.version}:${displayedRange}:${selectionKey}`} result={local.value} symbols={listedSymbols} symbol={costSymbol} busy={costSymbol !== chartSymbol} onChange={selectSymbol} />}
        <Tooltip content={data ? <dl className="space-y-1 text-xs">
          <div><dt className="inline text-ink-3">数据覆盖：</dt><dd className="inline">{data.baseline ? shanghaiLabel(data.baseline) : '—'} → {data.end ? shanghaiLabel(data.end) : '—'}</dd></div>
          <div><dt className="inline text-ink-3">有效回测记录：</dt><dd className="inline">{data.backtest_included ? `${backtestMode === 'target' ? data.target_used_record_count ?? 0 : data.used_record_count}/${data.observation_count}` : 0}</dd></div>
          <div><dt className="inline text-ink-3">历史记录：</dt><dd className="inline">{data.record_count}</dd></div>
        </dl> : <span>{metadata?.data_until ? `数据截止 ${shanghaiLabel(metadata.data_until)}` : '暂无绩效快照'}</span>} arrow>
          <button type="button" aria-label="绩效数据说明" className="inline-flex h-[30px] w-6 min-h-0! shrink-0 items-center justify-center rounded text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent"><Info size={15} aria-hidden="true" /></button>
        </Tooltip>
      </div>
    </div>
    <div role="group" aria-label="绩效与品种贡献共用筛选" data-testid="performance-shared-controls" className="flex w-full min-w-0 flex-wrap items-center gap-x-1 gap-y-1 text-xs text-ink-2 [&_button]:min-h-0!">
      <fieldset><legend className="sr-only">绩效时间范围</legend><Segmented size="sm" className="rounded-md! [&_button]:h-[26px] [&_button]:px-2 [&_button]:py-0" value={range} options={RANGES} onChange={setRange} /></fieldset>
      <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1" role="group" aria-label="分析区间">
        {[{ label: '起点', value: intervalStart, other: intervalEnd }, { label: '终点', value: intervalEnd, other: intervalStart }]
          .map(({ label, value, other }) => <ObservationEndpointPicker key={label} label={label} value={value} other={other}
            times={intervalTimes} onChange={time => {
              if (other == null) return
              setSelectionNotice(false)
              const interval = intervalSelection(time, other)
              setPickerSelection(interval)
              setSelection(interval)
            }} />)}
        <button type="button" aria-label="取消选择" disabled={!selection} onClick={() => { setSelectionNotice(false); setSelection(null) }} className="inline-flex h-[30px] w-6 shrink-0 items-center justify-center rounded hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-40"><X size={15} aria-hidden="true" /></button>
      </div>
      <fieldset><legend className="sr-only">横轴尺度</legend><Segmented size="sm" className="rounded-md! [&_button]:h-[26px] [&_button]:px-2 [&_button]:py-0" value={scale} options={SCALES} onChange={setScale} /></fieldset>
    </div>
    {controls}
    </div>
    <div className="pt-2">
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
    <p role="status" className="flex min-h-5 items-center text-xs text-ink-3" data-testid="performance-loading-status">{snapshot && loadingMessage}</p>
    {selectionNotice && <p role="status" className="py-1 text-xs text-ink-3">数据已更新，原选区已失效，请重新选择。</p>}
    <ErrorNotice intent="read" title="区间成本读取失败" error={intervalCosts.error} hasData={intervalCosts.data != null} variant="compact" onRetry={intervalCosts.refresh} />
    <ErrorNotice intent="read" title="费率试算不可用，当前保留正式曲线" error={dirty ? feeInputs.error ?? trial.error : null} variant="compact" onRetry={refreshFeeInputs} />
    {dirty && <p role="status" className="py-2 text-xs text-warn">费率试算 {fee} BP · 未保存 · {trialMessage}<button type="button" className="ml-3 text-accent hover:underline" onClick={() => setDraft(null)}>取消试算</button></p>}
    <ErrorNotice intent="read" error={inputs.error ?? local.error} title="滑点数据读取失败" onRetry={refreshInputs} variant="compact" />
    <ErrorNotice intent="read" error={analysisError} title="分析组件加载失败" onRetry={() => void analysisWasm.download()} variant="compact" />
    <div data-testid="symbol-workbench" className="min-w-0 pb-4">
      <div key={chartKey} data-testid="performance-chart-stage" className="performance-chart-stage @container">
        {!snapshot || !chartData ? <PerformanceChartPlaceholder accountId={accountId} loading={backtestBusy || !metadata && !initialError} failed={!!initialError} message={loadingMessage} />
        : chartSymbol ? <SymbolPerformance chart={snapshot} rangeTarget={symbolRange} targetSamples={replaySnapshot === pendingTarget ? replay.value : null} selection={selection} slideInterval={slideInterval} symbol={chartSymbol} samples={chartSamples} currency={item?.currency ?? null} scaleMode={scale}
          accountId={accountId} channel={account.data?.trade_channel ?? item?.trade_channel} showExecutions={markers}
          heading={<div className="flex flex-wrap items-center gap-x-3">
            <h2 className="font-medium">{chartSymbol} · 绩效贡献</h2>
            <SymbolSlippage result={local.value} symbol={chartSymbol} side={costSide} onSelect={selectSymbol} />
          </div>}
          onInterval={(start, end) => setSelection({ kind: 'interval', start, end })} />
        : <PerformanceChart accountId={accountId} rangeTarget={rangeTarget} snapshotId={snapshot.version} viewKey={`${accountId}:${displayedRange}`} data={chartData} executionIndex={snapshot.execution_index} daily={daily} backtestMode={backtestMode} scaleMode={scale} costs={costs} intervalCost={local.value?.summary ?? (intervalCosts.error || intervalCosts.loading ? null : intervalCosts.data?.summary ?? null)} onSelect={value => { setSelectionNotice(false); setSelection(value) }} selection={selection} slideInterval={slideInterval} portfolioNames={portfolioNames} showExecutions={markers} />}
      </div>
      {costSymbol && <ErrorNotice intent="read" error={feeInputs.error ?? replay.error} title="品种回测读取失败" onRetry={refreshFeeInputs} variant="compact" />}
      <p role="status" className="min-h-5 text-xs text-ink-2">{costSymbol === chartSymbol && contributionQuery && !replay.error && !feeInputs.error
        && (replay.value?.length === 0 ? '区间内无有效回测观测' : !replay.value && analysisReady ? '正在读取品种回测' : '')}</p>
    </div>
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
