import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router'
import { ChevronLeft, ChevronRight, Circle, Crosshair, Info, Minus, Pause, Play, RefreshCw, RotateCcw, SkipBack, SkipForward, X } from 'lucide-react'
import { AccountPageTitle } from '@/features/account/pageHead'
import { useDomainStore } from '@/stores/domain'
import { Select } from '@/components/ui/Select'
import { Segmented } from '@/components/ui/Segmented'
import { ValueSlider } from '@/components/ui/ValueSlider'
import { Tooltip } from '@/components/ui/Tooltip'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { getReplayManifest, ReplayWindowBudgetError } from '@/lib/api/replay'
import { projectReplay } from '@/features/replay/model'
import { replayTradeKey } from '@/features/replay/chartInteraction'
import { ReplayChart } from '@/features/replay/ReplayChart'
import { appliedEventIndex, initialReplayCursor, playbackCursor } from '@/features/replay/playback'
import { replayOwnership } from '@/features/replay/ownership'
import { OrdersAndTrades, type ReplayDetailTab } from '@/features/replay/ReplayDetails'
import { ReplayStage } from '@/features/replay/ReplayStage'
import { OverflowText } from '@/components/ui/OverflowText'
import { InkRewrite } from '@/components/ui/InkRewrite'
import '@/features/replay/replay.css'
import { rememberReplaySymbols, replayInstrument, replayNumber, replayRange, replayExecutionRange, replayStream, replaySymbols, replayTime, replayWindowRequest, replayWindowDuration, smallerReplayRange } from '@/features/replay/presentation'
import { useReplayWindow } from '@/features/replay/useReplayWindow'
import type { ReplayCursor, ReplayManifest, ReplayRange } from '@/features/replay/types'

const buttonClass = 'inline-flex min-h-10 items-center justify-center gap-1.5 rounded-chip border border-line px-3 text-[13px] text-ink-2 hover:bg-fill disabled:cursor-default disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
const selectClass = 'min-h-9 max-w-full justify-between px-3 text-[13px]'
const availabilityLabels: Record<string, string> = { available: '可用', partial: '部分覆盖', unavailable: '不可用', not_recorded: '未记录', unsupported: '暂不支持' }
const environmentLabels: Record<string, string> = { live: '实盘', demo: '演示', sim: '本地模拟', kq: '快期模拟' }
const positionModeLabels: Record<string, string> = { net: '净持仓', hedge: '双向持仓' }

export function ExecutionReplayPage() {
  const { id, executionId = '' } = useParams(), accountId = Number(id)
  const item = useDomainStore(state => state.accounts?.find(account => account.account_id === accountId))
  const [manifestValue, setManifest] = useState<ReplayManifest | null>(null), [manifestError, setManifestError] = useState<Error | null>(null)
  const manifest = manifestValue?.account_id === accountId && manifestValue.execution_id === executionId ? manifestValue : null
  const [manifestLoading, setManifestLoading] = useState(true), [revision, setRevision] = useState(0)
  const [recordingId, setRecordingId] = useState(''), [symbol, setSymbol] = useState(''), [range, setRange] = useState<ReplayRange | null>(null)
  const [knownSymbols, setKnownSymbols] = useState<Record<string, string[]>>({})
  const [cursor, setCursor] = useState<ReplayCursor>({ time_us: 0 }), [playing, setPlaying] = useState(false), [speed, setSpeed] = useState('1')
  const [cursorWindow, setCursorWindow] = useState('')
  const [selectedOrder, setSelectedOrder] = useState<string | null>(null), [windowDuration, setWindowDuration] = useState('10')
  const [adaptiveDuration, setAdaptiveDuration] = useState<number | null>(null)
  const [selectedTrade, setSelectedTrade] = useState<string | null>(null)
  const [detailTab, setDetailTab] = useState<ReplayDetailTab>('orders')
  const [seekDraft, setSeekDraft] = useState<number | null>(null)
  const pendingSeek = useRef<{ recording: string; symbol: string; time_us: number } | null>(null)
  const continuePlayback = useRef<string | null>(null)
  const budgetRetries = useRef(0)
  const initialWindowDuration = useRef(windowDuration); initialWindowDuration.current = windowDuration
  useEffect(() => {
    const controller = new AbortController()
    continuePlayback.current = null; budgetRetries.current = 0
    setManifestLoading(true); setManifest(null); setManifestError(null); setPlaying(false); setRange(null); setRecordingId(''); setSymbol(''); setKnownSymbols({}); setAdaptiveDuration(null)
    void getReplayManifest(executionId, controller.signal).then(value => {
      if (controller.signal.aborted) return
      if (value.account_id !== accountId || value.execution_id !== executionId) throw new Error('这次执行不属于当前账户')
      setManifest(value)
      const duration = replayWindowDuration(initialWindowDuration.current)
      const first = value.recordings.find(recording => replayExecutionRange(recording.coverage, value.execution_start_us, duration)) ?? value.recordings[0]
      if (first) { setRecordingId(first.recording_id); setSymbol(replaySymbols(first)[0] ?? ''); setRange(replayExecutionRange(first.coverage, value.execution_start_us, duration) ?? replayRange(first.coverage, undefined, duration)) }
    }).catch(cause => { if (!controller.signal.aborted) setManifestError(cause instanceof Error ? cause : new Error(String(cause))) }).finally(() => { if (!controller.signal.aborted) setManifestLoading(false) })
    return () => controller.abort()
  }, [accountId, executionId, revision])

  const recording = manifest?.recordings.find(item => item.recording_id === recordingId)
  const request = useMemo(() => range && recording ? replayWindowRequest(recording, range, symbol) : null, [range, recording, symbol])
  const loaded = useReplayWindow(executionId, request, revision)
  const effectiveDuration = Math.min(replayWindowDuration(windowDuration), adaptiveDuration ?? Number.MAX_SAFE_INTEGER)
  const retryingBudget = loaded.error instanceof ReplayWindowBudgetError && budgetRetries.current < 16 && range != null && range.end_us - range.start_us > 1
  useEffect(() => {
    if (!(loaded.error instanceof ReplayWindowBudgetError) || !range || budgetRetries.current >= 16) return
    const pending = pendingSeek.current
    const target = pending?.recording === recordingId && pending.symbol === symbol ? pending.time_us : null
    const next = smallerReplayRange(range, target)
    if (!next) return
    budgetRetries.current += 1
    setAdaptiveDuration(previous => Math.min(previous ?? Number.MAX_SAFE_INTEGER, next.end_us - next.start_us))
    setRange(next)
  }, [loaded.error, range, recordingId, symbol])
  const windowData = loaded.value
  const context = windowData?.context ?? recording?.context
  const symbols = recording ? replaySymbols(recording, knownSymbols[recordingId] ?? []) : []
  useEffect(() => {
    if (!windowData) return
    budgetRetries.current = 0
    const discovered = replaySymbols(windowData)
    setKnownSymbols(previous => rememberReplaySymbols(previous, windowData))
    if (!symbol && discovered[0]) setSymbol(discovered[0])
  }, [windowData, symbol])
  const reviewed = useMemo(() => {
    try { return { value: windowData && range ? projectReplay(windowData, { time_us: range.end_us - 1 }, symbol) : null, error: null } }
    catch (cause) { return { value: null, error: cause instanceof Error ? cause : new Error(String(cause)) } }
  }, [windowData, range, symbol])
  const requestedSeek = pendingSeek.current?.recording === recordingId && pendingSeek.current?.symbol === symbol ? pendingSeek.current.time_us : null
  const defaultCursor = useMemo(() => windowData && reviewed.value && range ? initialReplayCursor(windowData, reviewed.value, symbol, range) : { time_us: range?.start_us ?? 0 }, [windowData, reviewed.value, symbol, range])
  const initialCursor = useMemo(() => range && requestedSeek != null && requestedSeek >= range.start_us && requestedSeek < range.end_us ? { time_us: requestedSeek } : continuePlayback.current ? { time_us: range?.start_us ?? 0, capture_seq: Number(windowData?.state_before_start?.state_through_seq ?? 0) } : defaultCursor, [range, requestedSeek, windowData, defaultCursor])
  const windowKey = windowData ? `${windowData.window_id}:${symbol}` : ''
  const currentCursor = useMemo(() => windowData && range && (cursorWindow !== windowKey || requestedSeek != null) ? initialCursor : cursor, [windowData, range, windowKey, cursorWindow, requestedSeek, initialCursor, cursor])
  const projected = useMemo(() => {
    try { return { value: windowData ? projectReplay(windowData, currentCursor, symbol) : null, error: null } }
    catch (cause) { return { value: null, error: cause instanceof Error ? cause : new Error(String(cause)) } }
  }, [windowData, currentCursor, symbol])
  const projection = projected.value
  const events = useMemo(() => windowData?.messages ?? [], [windowData])
  const eventIndex = appliedEventIndex(events, currentCursor)
  const cursorRef = useRef(currentCursor); cursorRef.current = currentCursor
  useEffect(() => {
    const hidden = () => { if (document.hidden) { continuePlayback.current = null; setPlaying(false) } }
    document.addEventListener('visibilitychange', hidden)
    hidden()
    return () => document.removeEventListener('visibilitychange', hidden)
  }, [])
  useEffect(() => {
    if (!windowData) { setPlaying(false); setSelectedOrder(null); setSelectedTrade(null); return }
    if (range && (cursorWindow !== windowKey || requestedSeek != null)) {
      setPlaying(false); setSelectedOrder(null); setSelectedTrade(null)
      setCursorWindow(windowKey)
      setCursor(initialCursor)
      pendingSeek.current = null
      if (continuePlayback.current === windowData.recording_id) { continuePlayback.current = null; if (!document.hidden) setPlaying(true) }
    }
  }, [windowData, range, initialCursor, windowKey, cursorWindow, requestedSeek])

  const pauseAndSeek = useCallback((timeUs: number, captureSeq?: number) => {
    continuePlayback.current = null
    setPlaying(false); setSeekDraft(null)
    if (range) setCursor({ time_us: Math.max(range.start_us, Math.min(range.end_us - 1, Math.round(timeUs))), capture_seq: captureSeq })
  }, [range])
  const seekRecording = (timeUs: number) => {
    continuePlayback.current = null; budgetRetries.current = 0
    setPlaying(false); setSeekDraft(null)
    if (!recording || !range) return
    const coverage = recording.coverage.find(item => timeUs >= item.start_us && timeUs < item.end_us)
    if (!coverage) return
    if (windowData && !loaded.error && !projected.error && timeUs >= range.start_us && timeUs < range.end_us) { setCursor({ time_us: timeUs }); return }
    pendingSeek.current = { recording: recordingId, symbol, time_us: timeUs }
    const duration = effectiveDuration
    const start = !windowData || loaded.error || projected.error ? timeUs : coverage.start_us + Math.floor((timeUs - coverage.start_us) / duration) * duration
    setRange(replayRange(recording.coverage, start, duration))
  }
  const step = useCallback((direction: -1 | 1) => {
    continuePlayback.current = null
    setPlaying(false)
    if (!range || !windowData || projected.error) return
    const index = Math.min(events.length - 1, appliedEventIndex(events, cursorRef.current) + direction)
    if (index < 0) setCursor({ time_us: range.start_us, capture_seq: Number(windowData.state_before_start?.state_through_seq ?? 0) })
    else setCursor({ time_us: events[index].time_us, capture_seq: events[index].capture_seq })
  }, [events, range, windowData, projected.error])
  useEffect(() => {
    if (!playing || !windowData || !range || cursorWindow !== windowKey || projected.error || document.hidden) return
    let frame = 0
    const startWall = performance.now(), startUs = cursorRef.current.time_us
    let lastPublished = startWall - 1000 / 30
    const advance = (now: number) => {
      if (now - lastPublished < 1000 / 30) { frame = requestAnimationFrame(advance); return }
      lastPublished = now
      const target = Math.min(range.end_us - 1, Math.floor(startUs + (now - startWall) * 1000 * Number(speed)))
      const next = playbackCursor(windowData.messages, cursorRef.current, target)
      cursorRef.current = next; setCursor(next)
      if (next.time_us >= range.end_us - 1 && appliedEventIndex(windowData.messages, next) >= windowData.messages.length - 1) {
        setPlaying(false)
        const coverage = recording?.coverage.find(item => range.start_us >= item.start_us && range.start_us < item.end_us)
        if (coverage && range.end_us < coverage.end_us) {
          continuePlayback.current = windowData.recording_id
          setRange({ start_us: range.end_us, end_us: Math.min(coverage.end_us, range.end_us + effectiveDuration) })
        }
      }
      else frame = requestAnimationFrame(advance)
    }
    frame = requestAnimationFrame(advance)
    return () => cancelAnimationFrame(frame)
  }, [playing, speed, windowData, range, windowKey, cursorWindow, projected.error, recording, effectiveDuration])

  const selectRecording = (value: string) => {
    continuePlayback.current = null
    setPlaying(false); pendingSeek.current = null; setRecordingId(value); setAdaptiveDuration(null); budgetRetries.current = 0
    const next = manifest?.recordings.find(item => item.recording_id === value)
    if (!next) return
    setSymbol(replaySymbols(next, knownSymbols[value] ?? [])[0] ?? ''); setRange(replayRange(next.coverage, undefined, replayWindowDuration(windowDuration)))
  }
  const moveWindow = (direction: -1 | 1) => {
    continuePlayback.current = null; budgetRetries.current = 0
    if (!recording || !range) return
    setPlaying(false)
    const duration = effectiveDuration
    const coverageIndex = recording.coverage.findIndex(item => range.start_us >= item.start_us && range.start_us < item.end_us)
    const containing = recording.coverage[coverageIndex]
    if (!containing) return
    const target = direction > 0
      ? range.end_us < containing.end_us ? range.end_us : recording.coverage[coverageIndex + 1]?.start_us
      : range.start_us > containing.start_us ? Math.max(containing.start_us, range.start_us - duration) : recording.coverage[coverageIndex - 1] ? Math.max(recording.coverage[coverageIndex - 1].start_us, recording.coverage[coverageIndex - 1].end_us - duration) : undefined
    if (target == null) return
    setRange(replayRange(recording.coverage, target, duration))
  }
  const unit = context ? replayInstrument(context, symbol) : undefined
  const target = projection?.targetsByExecution[executionId]?.[symbol]
  const ownQuantity = projection?.executionSymbolComplete[executionId]?.[symbol] ? projection.executionTotals[executionId]?.[symbol] ?? 0 : null
  const warnings = [...new Set([...(projection?.warnings ?? []), ...(windowData?.gaps ?? recording?.gaps ?? []).map(gap => gap.reason ?? '存在记录缺口')])]
  const canPrevWindow = Boolean(range && recording?.coverage.some(item => item.start_us < range.start_us))
  const canNextWindow = Boolean(range && recording?.coverage.some(item => item.end_us > range.end_us))
  const currentCoverage = recording?.coverage.find(item => range && range.start_us >= item.start_us && range.start_us < item.end_us)
  const readReady = projection != null && reviewed.value != null && windowData != null && !loaded.loading
  const navigationTime = projection ? currentCursor.time_us : range?.start_us ?? 0
  const visibleOrders = Object.values(projection?.orders ?? {}).filter(order => order.symbol === symbol)
  const activeOrder = selectedOrder && reviewed.value?.orders[selectedOrder] ? selectedOrder : visibleOrders.find(order => order.key === selectedOrder)?.key ?? visibleOrders.find(order => replayOwnership(order, executionId).kind === 'current')?.key ?? visibleOrders[0]?.key ?? null
  const inspect = () => { continuePlayback.current = null; setPlaying(false) }
  const inspectOrder = (key: string) => { inspect(); setSelectedOrder(key); setSelectedTrade(null); setDetailTab('orders') }
  const inspectTrade = (key: string) => {
    inspect(); setSelectedTrade(key); setDetailTab('trades')
    const trade = reviewed.value?.ownTrades.find(value => replayTradeKey(value) === key)
    const order = Object.values(reviewed.value?.orders ?? {}).find(value => trade?.order_id != null && value.order_id === trade.order_id)
    if (order) setSelectedOrder(order.key)
  }
  const firstQuote = events.find(event => event.topic === `/quote/${symbol}` && event.data.book_valid === true && event.data.available !== false)

  const playControls = range && <div className="replay-play-controls" role="group" aria-label="回放播放控制">
            <Tooltip content="前一观察"><button type="button" className={`${buttonClass} replay-transport-button`} onClick={() => step(-1)} disabled={!readReady || eventIndex < 0} aria-label="前一观察"><SkipBack size={14} aria-hidden /><span className="sr-only">上一步</span></button></Tooltip>
            <Tooltip content={playing ? '暂停回放' : '播放回放'}><button type="button" className={`${buttonClass} replay-transport-button replay-play-button`} aria-label={playing ? '暂停回放' : '播放回放'} aria-pressed={playing} onClick={() => { continuePlayback.current = null; if (!playing && navigationTime >= range.end_us - 1) pauseAndSeek(defaultCursor.time_us); setPlaying(value => !value) }} disabled={!readReady}>{playing ? <Pause size={14} aria-hidden /> : <Play size={14} aria-hidden />}<span className="sr-only"><InkRewrite text={playing ? '暂停' : '播放'} tone="label" /></span></button></Tooltip>
            <Tooltip content="后一观察"><button type="button" className={`${buttonClass} replay-transport-button`} onClick={() => step(1)} disabled={!readReady || eventIndex >= events.length - 1} aria-label="后一观察"><SkipForward size={14} aria-hidden /><span className="sr-only">下一步</span></button></Tooltip>
          </div>
  const transport = range && <div className="replay-chart-transport">
          <div role="group" aria-label="播放速度" className="replay-speed-picker"><Segmented size="sm" className="num" value={speed} onChange={setSpeed} options={['0.1', '0.25', '0.5', '1', '2'].map(value => ({ value, label: `${value}×` }))} /></div>
          <time className="replay-cursor-time num text-xs text-ink-1">{replayTime(navigationTime)} <span className="text-ink-2">{playing ? '播放中' : '已暂停'}</span></time>
          <span className="replay-target num text-xs text-ink-2">目标 {replayNumber(target, unit?.quantity_decimals)} / 本次成交 {replayNumber(ownQuantity, unit?.quantity_decimals)} {unit?.quantity_label}</span>

  </div>
  return <section className="replay-workbench" aria-label="执行回放">
    <header className="replay-desk-header">
      <div className="flex min-w-0 flex-wrap items-center gap-3"><AccountPageTitle accountId={accountId} page="执行回放" name={item?.name} channel={item?.trade_channel ?? context?.channel} market={item?.market} flip={false} back={{ to: `/accounts/${accountId}/executions/${executionId}`, label: '返回执行详情' }} /></div>
      <div className="replay-header-actions">
        <button type="button" popoverTarget="replay-information" className={buttonClass} aria-label="录制信息与完整性说明"><Info size={15} aria-hidden /><span>录制信息</span>{warnings.length > 0 && <span className="text-warn">{warnings.length}</span>}</button>
        <button type="button" onClick={() => setRevision(value => value + 1)} className={buttonClass} aria-label="重新读取已封口记录" disabled={manifestLoading || loaded.loading}><RefreshCw size={14} aria-hidden /></button>
      </div>
    </header>
    <div className="replay-read-errors" hidden={!manifestError && !loaded.error && !projected.error && !reviewed.error}>
      <ErrorNotice intent="read" title="回放目录读取失败" error={manifestError} onRetry={() => setRevision(value => value + 1)} />
      <ErrorNotice intent="read" title="回放窗口读取失败" error={(retryingBudget ? null : loaded.error) ?? projected.error ?? reviewed.error} onRetry={() => setRevision(value => value + 1)} />
    </div>
    {manifestLoading && <p className="replay-empty" role="status">正在查找这次执行的录制…</p>}
    {manifest && !manifest.recordings.length && <div className="replay-empty"><h2 className="font-medium text-ink-1">{availabilityLabels[manifest.availability] ?? '暂无可回放记录'}</h2><p className="mt-2 max-w-xl text-sm text-ink-2">{manifest.reason ?? '这次执行没有已封口的录制。账户录制默认关闭，开启前的数据不会补录。'}</p></div>}
    {recording && context && <>
      {projection && reviewed.value && windowData && !loaded.loading ? <OrdersAndTrades projection={projection} scene={reviewed.value} context={context} symbol={symbol} selected={activeOrder} onSelect={inspectOrder} selectedTradeKey={selectedTrade} onSelectTrade={inspectTrade} executionId={executionId} tab={detailTab} onTabChange={setDetailTab} onSeek={seekRecording} coverage={recording.coverage} gaps={windowData.gaps}>
      <div className="replay-main-chart">
        {loaded.loading && <div className="replay-empty" role="status">正在恢复所选时刻的状态，回放已暂停…</div>}
        {projection && windowData && range && !loaded.loading && <ReplayChart initialTimeUs={defaultCursor.time_us} window={windowData} range={range} projection={projection} scene={reviewed.value ?? projection} symbol={symbol} selectedOrder={activeOrder} onSelectOrder={inspectOrder} selectedTrade={selectedTrade} onSelectTrade={inspectTrade} onInspect={inspect} onSeek={pauseAndSeek} executionId={executionId} playControls={playControls} firstQuoteControl={<button type="button" className={`${buttonClass} replay-first-quote`} disabled={!readReady || !firstQuote} onClick={() => { if (firstQuote) pauseAndSeek(firstQuote.time_us) }}>跳到首个盘口</button>} symbolControl={symbols.length > 1 ? <Select ariaLabel="选择回放品种" searchable className={selectClass} value={symbol} options={symbols.map(value => ({ value, label: value }))} onChange={value => { continuePlayback.current = null; setPlaying(false); setSelectedOrder(null); setSelectedTrade(null); setSymbol(value) }} /> : <span className="text-[13px] font-semibold text-ink-1">{symbol || '无已录品种'}</span>}>{transport}</ReplayChart>}
        {!range && <p className="replay-empty">该录制段没有可读取的封口覆盖区间。</p>}
        {!loaded.loading && range && !readReady && <p className="replay-empty text-ink-2">所选窗口暂不可用。可在录制信息中切换窗口或重试。</p>}
      </div>
      </OrdersAndTrades> : <ReplayStage><div className="replay-chart-column"><div className="replay-chart-area"><div className="replay-main-chart">
        <div className="replay-chart-playback"><span className="text-[13px] font-semibold text-ink-1">{symbol || '品种未知'}</span>{transport}</div>
        <div className="replay-chart-meta"><div className="replay-chart-legend"><span className="replay-depth-bid"><Minus size={14} aria-hidden />买一</span><span className="replay-depth-ask"><Minus size={14} aria-hidden />卖一</span><span><Minus size={14} aria-hidden />成交价</span><span><Circle size={10} aria-hidden />市场成交</span><span className="replay-order-color"><Minus size={14} aria-hidden />账户委托</span><span><Minus size={14} aria-hidden />未到达</span></div><div className="replay-chart-view-controls replay-chart-navigation"><div className="replay-chart-return-slot" /><div className="replay-playback-group">{playControls}<button type="button" className="min-h-7 min-w-7 text-ink-2 opacity-40" aria-label="恢复视图" disabled><RotateCcw size={15} aria-hidden /></button><button type="button" className="min-h-7 min-w-7 text-ink-2 opacity-40" aria-label="聚焦委托过程" disabled><Crosshair size={14} aria-hidden /></button><button type="button" className="min-h-7 min-w-7 text-ink-2 opacity-40" aria-label="图层与图例说明" disabled><Info size={15} aria-hidden /></button></div><button type="button" className={`${buttonClass} replay-first-quote`} disabled>跳到首个盘口</button></div></div>
        <div className="replay-empty" role="status">{loaded.loading || retryingBudget ? '正在恢复所选时刻的状态，回放已暂停…' : '所选窗口暂不可用，可在录制信息中切换窗口或重试。'}</div>
        {range && <div className="replay-chart-axis replay-loading-axis"><ValueSlider variant="axis" label="录制定位" min={0} max={Math.max(1, (currentCoverage?.end_us ?? range.end_us) - (currentCoverage?.start_us ?? range.start_us) - 1)} step={1} value={Math.max(0, (seekDraft ?? navigationTime) - (currentCoverage?.start_us ?? range.start_us))} formatValue={offset => replayTime((currentCoverage?.start_us ?? range.start_us) + offset)} onChange={offset => { continuePlayback.current = null; setPlaying(false); setSeekDraft((currentCoverage?.start_us ?? range.start_us) + offset) }} onChangeEnd={offset => seekRecording((currentCoverage?.start_us ?? range.start_us) + offset)} /></div>}
      </div></div><section className="replay-dock" aria-label="回放明细"><div className="replay-dock-header"><Segmented value={detailTab} options={[{ value: 'orders', label: '委托' }, { value: 'trades', label: '成交' }, { value: 'commands', label: '报撤观察' }, { value: 'positions', label: '持仓' }]} onChange={setDetailTab} size="sm" /></div></section></div></ReplayStage>}
    </>}
    <div id="replay-information" popover="auto" className="replay-information bg-surface text-ink-1" aria-label="录制信息与完整性说明">
      <div className="mb-4 flex items-center justify-between gap-4"><h2 className="text-sm font-medium">录制信息</h2><button type="button" popoverTarget="replay-information" popoverTargetAction="hide" className={buttonClass} aria-label="关闭录制信息"><X size={15} aria-hidden /></button></div>
      <p className="mb-3 break-all text-xs text-ink-2">账户 #{accountId} · 执行 {executionId}</p>
      {recording && context && <>
          <div className="replay-window-controls mb-4">
            <button type="button" className={buttonClass} onClick={() => moveWindow(-1)} disabled={!canPrevWindow} aria-label="上一窗口"><ChevronLeft size={14} aria-hidden /><span className="sr-only">上一窗口</span></button>
            <label><span>读取窗口 </span><Select value={windowDuration} ariaLabel="窗口时长" className={selectClass} onChange={value => { continuePlayback.current = null; budgetRetries.current = 0; setPlaying(false); setWindowDuration(value); setAdaptiveDuration(null); setRange(replayRange(recording.coverage, range?.start_us, replayWindowDuration(value))) }} options={['2', '5', '10', '20'].map(value => ({ value, label: `${value} 秒` }))} /></label>
            <button type="button" className={buttonClass} onClick={() => moveWindow(1)} disabled={!canNextWindow} aria-label="下一窗口"><span className="sr-only">下一窗口</span><ChevronRight size={14} aria-hidden /></button>
          </div>
        <div className="mb-3 text-xs text-ink-2"><p className="mb-1">录制段</p><Select ariaLabel="选择录制段" className={`${selectClass} w-full`} value={recordingId} onChange={selectRecording} options={(manifest?.recordings ?? []).map((value, index) => ({ value: value.recording_id, label: `第 ${index + 1} 段${value.order_known === false ? '（先后未知）' : ''}` }))} /></div>
        <OverflowText text={recording.recording_id} className="mb-3 block text-xs text-ink-2" />
        {recording.coverage.length > 1 && <div className="mb-3 text-xs text-ink-2"><p className="mb-1">可用时段</p><Select ariaLabel="选择可用覆盖时段" className={`${selectClass} w-full`} value={currentCoverage?.start_us ?? null} onChange={value => { if (value == null) return; continuePlayback.current = null; setPlaying(false); pendingSeek.current = null; setRange(replayRange(recording.coverage, value, replayWindowDuration(windowDuration))) }} options={recording.coverage.map(value => ({ value: value.start_us, label: `${replayTime(value.start_us, true, false)} — ${replayTime(value.end_us, true, false)}` }))} /></div>}
        {recording.symbols_complete === false && <p className="mb-3 text-xs text-ink-2">品种目录尚不完整，后续窗口继续补充。</p>}
        <p className="mb-3 text-xs text-ink-2">{environmentLabels[context.environment ?? ''] ?? context.environment ?? '环境未知'} · {context.account_mode ?? '账户模式未知'} · {positionModeLabels[context.position_mode ?? ''] ?? '持仓模式未知'} · 上海时间</p>
        <div className="space-y-2 border-t border-line py-3 text-xs text-ink-2">{['depth', 'market_trade', 'own_order', 'own_trade', 'position'].map((name, index) => { const stream = replayStream(context, symbol, name); return <p key={name}>{['盘口', '市场成交', '自有委托', '自有成交', '持仓'][index]}：{stream ? availabilityLabels[stream.availability] : '未说明'}{stream?.reason ? ` · ${stream.reason === 'waiting_observation' ? '等待观察' : stream.reason}` : ''}{stream?.delivery ? ` · ${stream.delivery}` : ''}</p> })}</div>
        {warnings.length > 0 && <div className="mb-3 space-y-2 text-xs text-warn" role="status">{warnings.map(warning => <p key={warning}>{warning}</p>)}</div>}
        <p className="border-t border-line pt-3 text-xs text-ink-2">展示已读取时段的完整记录，未来记录灰显为未到达；委托当前状态按游标恢复。超出读取预算时可选择较短窗口。热力颜色表示挂量，有限盘口不代表排队位置；完整性与缺失以本次录制说明为准。</p>
      </>}
    </div>
  </section>
}
