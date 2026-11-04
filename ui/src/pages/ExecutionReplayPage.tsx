import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useParams } from 'react-router'
import { Circle, Crosshair, Info, Minus, Pause, Play, RefreshCw, RotateCcw, SkipBack, SkipForward } from 'lucide-react'
import { AccountPageTitle } from '@/features/account/pageHead'
import { useDomainStore } from '@/stores/domain'
import { Select } from '@/components/ui/Select'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { getReplayManifest, readReplayExecution } from '@/lib/api/replay'
import { currentReplayQuote } from '@/features/replay/chartGeometry'
import { replayExecutionAt, replayExecutionScene } from '@/features/replay/executionScene'
import { replayTradeKey } from '@/features/replay/chartInteraction'
import { ReplayChart } from '@/features/replay/ReplayChart'
import { appliedEventIndex, playbackCursor } from '@/features/replay/playback'
import { replayOwnership } from '@/features/replay/ownership'
import { OrdersAndTrades, type ReplayDetailTab } from '@/features/replay/ReplayDetails'
import { ReplayStage } from '@/features/replay/ReplayStage'
import { replayExecutionPlan, replayInstrument, replayNumber, replaySymbols, replayTime } from '@/features/replay/presentation'
import type { ReplayCursor, ReplayManifest, ReplayRange, ReplayWindow } from '@/features/replay/types'
import '@/features/replay/replay.css'

const buttonClass = 'inline-flex min-h-10 items-center justify-center gap-1.5 rounded-chip border border-line px-3 text-[13px] text-ink-2 hover:bg-fill disabled:cursor-default disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
const selectClass = 'min-h-9 max-w-full justify-between px-3 text-[13px]'
const availabilityLabels: Record<string, string> = { available: '可用', partial: '部分覆盖', unavailable: '不可用', not_recorded: '未记录', unsupported: '暂不支持' }

type LoadedPart = { range: ReplayRange; window: ReplayWindow }
type LoadedExecution = { key: string; value: LoadedPart[] | null; error: Error | null; loading: boolean }

export function ExecutionReplayPage() {
  const { id, executionId = '' } = useParams(), accountId = Number(id)
  const location = useLocation()
  const journalPath = `/accounts/${accountId}/executions`
  const detailPath = `${journalPath}/${executionId}`
  const requestedReturn = (location.state as { replayReturn?: { to?: string; state?: { journalReturn?: string; performanceReturn?: string } } } | null)?.replayReturn
  const fromJournal = typeof requestedReturn?.to === 'string' && (requestedReturn.to === journalPath || requestedReturn.to.startsWith(`${journalPath}?`))
  const detailState = requestedReturn?.to === detailPath ? requestedReturn.state : undefined
  const journalReturn = typeof detailState?.journalReturn === 'string' && (detailState.journalReturn === journalPath || detailState.journalReturn.startsWith(`${journalPath}?`)) ? detailState.journalReturn : undefined
  const performanceReturn = detailState?.performanceReturn === `/accounts/${accountId}/history` ? detailState.performanceReturn : undefined
  const back = fromJournal ? { to: requestedReturn!.to!, label: '返回执行记录' }
    : { to: detailPath, label: '返回执行详情', state: detailState ? { journalReturn, performanceReturn } : undefined }
  const item = useDomainStore(state => state.accounts?.find(account => account.account_id === accountId))
  const [manifestLoading, setManifestLoading] = useState(true), [revision, setRevision] = useState(0)
  const [manifestState, setManifest] = useState<{ revision: number; value: ReplayManifest } | null>(null), [manifestError, setManifestError] = useState<Error | null>(null)
  const manifestValue = manifestState?.revision === revision ? manifestState.value : null
  const manifest = manifestValue?.account_id === accountId && manifestValue.execution_id === executionId ? manifestValue : null
  const [symbol, setSymbol] = useState('')
  const [cursor, setCursor] = useState<ReplayCursor>({ time_us: 0 }), [playing, setPlaying] = useState(false), [speed, setSpeed] = useState('1')
  const [selectedOrder, setSelectedOrder] = useState<string | null>(null), [selectedTrade, setSelectedTrade] = useState<string | null>(null)
  const [detailTab, setDetailTab] = useState<ReplayDetailTab>('orders')
  const [loadedState, setLoaded] = useState<LoadedExecution>({ key: '', value: null, error: null, loading: false })

  useEffect(() => {
    const controller = new AbortController()
    setManifestLoading(true); setManifest(null); setManifestError(null); setPlaying(false); setSymbol('')
    setSelectedOrder(null); setSelectedTrade(null)
    void getReplayManifest(executionId, controller.signal).then(value => {
      if (controller.signal.aborted) return
      if (value.account_id !== accountId || value.execution_id !== executionId) throw new Error('这次执行不属于当前账户')
      setManifest({ revision, value })
      setSymbol(value.recordings.flatMap(recording => replaySymbols(recording))[0] ?? '')
      setCursor({ time_us: replayExecutionPlan(value)?.range.start_us ?? 0 })
    }).catch(cause => { if (!controller.signal.aborted) setManifestError(cause instanceof Error ? cause : new Error(String(cause))) })
      .finally(() => { if (!controller.signal.aborted) setManifestLoading(false) })
    return () => controller.abort()
  }, [accountId, executionId, revision])

  const plan = useMemo(() => manifest ? replayExecutionPlan(manifest) : null, [manifest])
  const range = plan?.range ?? null
  const key = plan ? JSON.stringify([accountId, executionId, symbol, revision]) : ''
  useEffect(() => {
    const controller = new AbortController()
    if (!plan || !key) return () => controller.abort()
    setLoaded({ key, value: null, error: null, loading: true })
    void readReplayExecution(executionId, plan.segments, symbol, controller.signal).then(value => {
      if (!controller.signal.aborted) setLoaded({ key, value, error: null, loading: false })
    }).catch(cause => {
      if (!controller.signal.aborted) setLoaded({ key, value: null, error: cause instanceof Error ? cause : new Error(String(cause)), loading: false })
    })
    return () => controller.abort()
  }, [executionId, key, plan, symbol])
  const loaded = loadedState.key === key ? loadedState : { key, value: null, error: null, loading: Boolean(key) }

  const symbols = useMemo(() => {
    const values = new Set(manifest?.recordings.flatMap(recording => replaySymbols(recording)) ?? [])
    for (const part of loaded.value ?? []) for (const value of replaySymbols(part.window)) values.add(value)
    return [...values]
  }, [manifest, loaded.value])
  useEffect(() => { if (!symbol && symbols[0]) setSymbol(symbols[0]) }, [symbol, symbols])

  const overview = useMemo(() => {
    try { return { value: loaded.value && plan ? replayExecutionScene(loaded.value, plan.range, plan.gaps) : null, error: null } }
    catch (cause) { return { value: null, error: cause instanceof Error ? cause : new Error(String(cause)) } }
  }, [loaded.value, plan])
  const projectionResult = useMemo(() => {
    try { return { value: overview.value ? replayExecutionAt(overview.value, cursor, symbol) : null, error: null } }
    catch (cause) { return { value: null, error: cause instanceof Error ? cause : new Error(String(cause)) } }
  }, [overview.value, cursor, symbol])
  const projection = projectionResult.value
  const slices = useMemo(() => overview.value?.slices ?? [], [overview.value])
  const active = slices.filter(slice => cursor.time_us >= slice.range.start_us && cursor.time_us < slice.range.end_us)
  const context = active.length === 1 ? active[0].window.context : slices[0]?.window.context ?? manifest?.recordings[0]?.context
  const windowData = useMemo(() => {
    const first = overview.value?.slices[0]?.window
    return first && context ? { ...first, window_id: 'execution:' + executionId + ':' + symbol, context, gaps: overview.value!.gaps,
      coverage: overview.value!.slices.map(slice => slice.range) } : null
  }, [context, executionId, overview.value, symbol])
  const events = useMemo(() => slices.flatMap(slice => slice.window.messages).sort((a, b) => a.time_us - b.time_us || a.capture_seq - b.capture_seq), [slices])
  const overlapping = useMemo(() => slices.some((slice, index) => slices.slice(index + 1).some(other =>
    other.range.start_us < slice.range.end_us && other.range.end_us > slice.range.start_us)), [slices])
  const eventIndex = appliedEventIndex(events, cursor)
  const cursorRef = useRef(cursor); cursorRef.current = cursor
  const readReady = Boolean(projection && overview.value && windowData && range && !loaded.loading)

  useEffect(() => {
    const hidden = () => { if (document.hidden) setPlaying(false) }
    document.addEventListener('visibilitychange', hidden); hidden()
    return () => document.removeEventListener('visibilitychange', hidden)
  }, [])
  useEffect(() => { if (!readReady) setPlaying(false) }, [readReady])

  const pauseAndSeek = useCallback((timeUs: number, captureSeq?: number) => {
    setPlaying(false)
    if (range) setCursor({ time_us: Math.max(range.start_us, Math.min(range.end_us - 1, Math.round(timeUs))), capture_seq: captureSeq })
  }, [range])
  const step = useCallback((direction: -1 | 1) => {
    setPlaying(false)
    if (!range || !readReady || overlapping) return
    const index = Math.min(events.length - 1, appliedEventIndex(events, cursorRef.current) + direction)
    if (index < 0) setCursor({ time_us: range.start_us })
    else setCursor({ time_us: events[index].time_us, capture_seq: events[index].capture_seq })
  }, [events, range, readReady, overlapping])

  useEffect(() => {
    if (!playing || !readReady || !range || document.hidden) return
    let frame = 0
    const startWall = performance.now(), startUs = cursorRef.current.time_us
    let lastPublished = startWall - 1000 / 30
    const advance = (now: number) => {
      if (now - lastPublished < 1000 / 30) { frame = requestAnimationFrame(advance); return }
      lastPublished = now
      const target = Math.min(range.end_us - 1, Math.floor(startUs + (now - startWall) * 1000 * Number(speed)))
      const next = overlapping ? { time_us: target } : playbackCursor(events, cursorRef.current, target)
      cursorRef.current = next; setCursor(next)
      if (next.time_us >= range.end_us - 1 && (overlapping || appliedEventIndex(events, next) >= events.length - 1)) setPlaying(false)
      else frame = requestAnimationFrame(advance)
    }
    frame = requestAnimationFrame(advance)
    return () => cancelAnimationFrame(frame)
  }, [playing, readReady, range, speed, events, overlapping])

  const scene = overview.value?.scene
  const unit = context ? replayInstrument(context, symbol) : undefined
  const target = projection?.targetsByExecution[executionId]?.[symbol]
  const ownQuantity = projection?.executionSymbolComplete[executionId]?.[symbol] ? projection.executionTotals[executionId]?.[symbol] ?? 0 : null
  const visibleOrders = Object.values(projection?.orders ?? {}).filter(order => order.symbol === symbol)
  const activeOrder = selectedOrder && scene?.orders[selectedOrder] ? selectedOrder : visibleOrders.find(order => order.key === selectedOrder)?.key
    ?? visibleOrders.find(order => replayOwnership(order, executionId).kind === 'current')?.key ?? visibleOrders[0]?.key ?? null
  const inspect = () => setPlaying(false)
  const inspectOrder = (orderKey: string) => { inspect(); setSelectedOrder(orderKey); setSelectedTrade(null); setDetailTab('orders') }
  const inspectTrade = (tradeKey: string) => {
    inspect(); setSelectedTrade(tradeKey); setDetailTab('trades')
    const trade = scene?.ownTrades.find(value => replayTradeKey(value) === tradeKey)
    const order = Object.values(scene?.orders ?? {}).find(value => trade?.order_id != null && value.order_id === trade.order_id)
    if (order) setSelectedOrder(order.key)
  }
  const firstQuote = scene?.quoteHistory.find(quote => quote.symbol === symbol && currentReplayQuote(quote, quote.time_us, windowData?.gaps ?? []))
  const stateLabel = !readReady ? '' : active.length > 1 ? '录制重叠，状态未知' : active.length === 0 ? '当前时刻未录制' : ''

  const playControls = range && <div className="replay-play-controls" role="group" aria-label="回放播放控制">
    <Tooltip content="前一观察"><button type="button" className={buttonClass + ' replay-transport-button'} onClick={() => step(-1)} disabled={!readReady || overlapping || eventIndex < 0} aria-label="前一观察"><SkipBack size={14} aria-hidden /><span className="sr-only">上一步</span></button></Tooltip>
    <Tooltip content={playing ? '暂停回放' : '播放回放'}><button type="button" className={buttonClass + ' replay-transport-button replay-play-button'} aria-label={playing ? '暂停回放' : '播放回放'} aria-pressed={playing} onClick={() => { if (!playing && cursor.time_us >= range.end_us - 1) setCursor({ time_us: range.start_us }); setPlaying(value => !value) }} disabled={!readReady}>{playing ? <Pause size={14} aria-hidden /> : <Play size={14} aria-hidden />}<span className="sr-only"><InkRewrite text={playing ? '暂停' : '播放'} tone="label" /></span></button></Tooltip>
    <Tooltip content="后一观察"><button type="button" className={buttonClass + ' replay-transport-button'} onClick={() => step(1)} disabled={!readReady || overlapping || eventIndex >= events.length - 1} aria-label="后一观察"><SkipForward size={14} aria-hidden /><span className="sr-only">下一步</span></button></Tooltip>
  </div>
  const transport = range && <div className="replay-chart-transport">
    <div role="group" aria-label="播放速度" className="replay-speed-picker"><Segmented size="sm" className="num" value={speed} onChange={setSpeed} options={['0.1', '0.25', '0.5', '1', '2'].map(value => ({ value, label: value + '×' }))} /></div>
    <span className="text-xs text-ink-2">{playing ? '播放中' : '已暂停'}</span>
    {stateLabel && <span className="text-xs text-warn" role="status">{stateLabel}</span>}
    <span className="replay-target num text-xs text-ink-2">目标 {replayNumber(target, unit?.quantity_decimals)} / 本次成交 {replayNumber(ownQuantity, unit?.quantity_decimals)} {unit?.quantity_label}</span>
  </div>

  return <section className="replay-workbench" aria-label="执行回放">
    <header className="replay-desk-header">
      <div className="flex min-w-0 flex-wrap items-center gap-3"><AccountPageTitle accountId={accountId} page="执行回放" name={item?.name} channel={item?.trade_channel ?? context?.channel} market={item?.market} flip={false} back={back} /></div>
      <div className="replay-header-actions"><button type="button" onClick={() => setRevision(value => value + 1)} className={buttonClass} aria-label="重新读取已封口记录" disabled={manifestLoading || loaded.loading}><RefreshCw size={14} aria-hidden /></button></div>
    </header>
    <div className="replay-read-errors" hidden={!manifestError && !loaded.error && !overview.error && !projectionResult.error}>
      <ErrorNotice intent="read" title="回放目录读取失败" error={manifestError} onRetry={() => setRevision(value => value + 1)} />
      <ErrorNotice intent="read" title="执行回放读取失败" error={loaded.error ?? overview.error ?? projectionResult.error} onRetry={() => setRevision(value => value + 1)} />
    </div>
    {manifestLoading && <p className="replay-empty" role="status">正在查找这次执行的录制…</p>}
    {manifest && !manifest.recordings.length && <div className="replay-empty"><h2 className="font-medium text-ink-1">{availabilityLabels[manifest.availability] ?? '暂无可回放记录'}</h2><p className="mt-2 max-w-xl text-sm text-ink-2">{manifest.reason ?? '这次执行没有已封口的录制。账户录制默认关闭，开启前的数据不会补录。'}</p></div>}
    {manifest && manifest.recordings.length > 0 && !plan && <p className="replay-empty">本次执行没有可读取的录制时段。</p>}
    {plan && !loaded.loading && !loaded.error && !overview.error && !overview.value && <p className="replay-empty">本次执行没有可读取的录制时段。</p>}
    {projection && scene && windowData && context && range && !loaded.loading ? <OrdersAndTrades projection={projection} scene={scene} context={context} symbol={symbol} selected={activeOrder} onSelect={inspectOrder} selectedTradeKey={selectedTrade} onSelectTrade={inspectTrade} executionId={executionId} tab={detailTab} onTabChange={setDetailTab} onSeek={pauseAndSeek} coverage={slices.map(slice => slice.range)} gaps={overview.value!.gaps}>
      <div className="replay-main-chart">
        <ReplayChart initialTimeUs={range.start_us} window={windowData} range={range} projection={projection} scene={scene} slices={slices} symbol={symbol} selectedOrder={activeOrder} onSelectOrder={inspectOrder} selectedTrade={selectedTrade} onSelectTrade={inspectTrade} onInspect={inspect} onSeek={pauseAndSeek} executionId={executionId} playControls={playControls} firstQuoteControl={<button type="button" className={buttonClass + ' replay-first-quote'} disabled={!readReady || !firstQuote} onClick={() => { if (firstQuote) pauseAndSeek(firstQuote.time_us, firstQuote.capture_seq) }}>跳到首个盘口</button>} symbolControl={symbols.length > 1 ? <Select ariaLabel="选择回放品种" searchable className={selectClass} value={symbol} options={symbols.map(value => ({ value, label: value }))} onChange={value => { setPlaying(false); setSelectedOrder(null); setSelectedTrade(null); setSymbol(value) }} /> : <span className="text-[13px] font-semibold text-ink-1">{symbol || '无已录品种'}</span>}>{transport}</ReplayChart>
      </div>
    </OrdersAndTrades> : plan && loaded.loading && <ReplayStage><div className="replay-chart-column"><div className="replay-chart-area"><div className="replay-main-chart">
      <div className="replay-chart-playback"><span className="text-[13px] font-semibold text-ink-1">{symbol || '品种未知'}</span>{transport}</div>
      <div className="replay-chart-meta"><div className="replay-chart-legend"><span className="replay-depth-bid"><Minus size={14} aria-hidden />买一</span><span className="replay-depth-ask"><Minus size={14} aria-hidden />卖一</span><span><Minus size={14} aria-hidden />成交价</span><span><Circle size={10} aria-hidden />市场成交</span><span className="replay-order-color"><Minus size={14} aria-hidden />账户委托</span><span><Minus size={14} aria-hidden />未到达</span></div><div className="replay-chart-view-controls replay-chart-navigation"><div className="replay-chart-return-slot" /><div className="replay-playback-group">{playControls}<button type="button" className="min-h-7 min-w-7 text-ink-2 opacity-40" aria-label="恢复视图" disabled><RotateCcw size={15} aria-hidden /></button><button type="button" className="min-h-7 min-w-7 text-ink-2 opacity-40" aria-label="聚焦委托过程" disabled><Crosshair size={14} aria-hidden /></button><button type="button" className="min-h-7 min-w-7 text-ink-2 opacity-40" aria-label="图层与图例说明" disabled><Info size={15} aria-hidden /></button></div><button type="button" className={buttonClass + ' replay-first-quote'} disabled>跳到首个盘口</button></div></div>
      <div className="replay-chart-plot"><div className="replay-empty h-full" role="status">正在读取整个执行的录制…</div><div className="replay-loading-axis" aria-hidden="true"><time className="replay-cursor-time num">{replayTime(cursor.time_us)}</time></div></div>
    </div></div><section className="replay-dock" aria-label="回放明细"><div className="replay-dock-header"><Segmented value={detailTab} options={[{ value: 'orders', label: '委托' }, { value: 'trades', label: '成交' }, { value: 'commands', label: '报撤观察' }, { value: 'positions', label: '持仓' }]} onChange={setDetailTab} size="sm" /></div></section></div></ReplayStage>}
  </section>
}
