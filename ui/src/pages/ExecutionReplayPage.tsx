import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router'
import { ChevronLeft, ChevronRight, Info, Pause, Play, RefreshCw, SkipBack, SkipForward, X } from 'lucide-react'
import { AccountPageTitle } from '@/features/account/pageHead'
import { useDomainStore } from '@/stores/domain'
import { Select } from '@/components/ui/Select'
import { ValueSlider } from '@/components/ui/ValueSlider'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { getReplayManifest } from '@/lib/api/replay'
import { projectReplay } from '@/features/replay/model'
import { replayTradeKey } from '@/features/replay/chartInteraction'
import { currentReplayQuote } from '@/features/replay/chartGeometry'
import { ReplayChart } from '@/features/replay/ReplayChart'
import { appliedEventIndex, playbackCursor } from '@/features/replay/playback'
import { replayOwnership } from '@/features/replay/ownership'
import { OrdersAndTrades, type ReplayDetailTab } from '@/features/replay/ReplayDetails'
import { OverflowText } from '@/components/ui/OverflowText'
import { InkRewrite } from '@/components/ui/InkRewrite'
import '@/features/replay/replay.css'
import { rememberReplaySymbols, replayInstrument, replayNumber, replayRange, replayStream, replaySymbols, replayTime, replayWindowRequest } from '@/features/replay/presentation'
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
  const [selectedOrder, setSelectedOrder] = useState<string | null>(null), [windowDuration, setWindowDuration] = useState('20')
  const [selectedTrade, setSelectedTrade] = useState<string | null>(null)
  const [detailTab, setDetailTab] = useState<ReplayDetailTab>('orders')
  const [seekDraft, setSeekDraft] = useState<number | null>(null)
  const pendingSeek = useRef<{ recording: string; symbol: string; time_us: number } | null>(null)
  const continuePlayback = useRef<string | null>(null)
  const initialWindowDuration = useRef(windowDuration); initialWindowDuration.current = windowDuration
  useEffect(() => {
    const controller = new AbortController()
    continuePlayback.current = null
    setManifestLoading(true); setManifest(null); setManifestError(null); setPlaying(false); setRange(null); setRecordingId(''); setSymbol(''); setKnownSymbols({})
    void getReplayManifest(executionId, controller.signal).then(value => {
      if (controller.signal.aborted) return
      if (value.account_id !== accountId || value.execution_id !== executionId) throw new Error('这次执行不属于当前账户')
      setManifest(value)
      const first = value.recordings[0]
      if (first) { setRecordingId(first.recording_id); setSymbol(replaySymbols(first)[0] ?? ''); setRange(replayRange(first.coverage, undefined, Number(initialWindowDuration.current) * 1_000_000)) }
    }).catch(cause => { if (!controller.signal.aborted) setManifestError(cause instanceof Error ? cause : new Error(String(cause))) }).finally(() => { if (!controller.signal.aborted) setManifestLoading(false) })
    return () => controller.abort()
  }, [accountId, executionId, revision])

  const recording = manifest?.recordings.find(item => item.recording_id === recordingId)
  const request = useMemo(() => range && recording ? replayWindowRequest(recording, range, symbol) : null, [range, recording, symbol])
  const loaded = useReplayWindow(executionId, request, revision)
  const windowData = loaded.value
  const context = windowData?.context ?? recording?.context
  const symbols = recording ? replaySymbols(recording, knownSymbols[recordingId] ?? []) : []
  useEffect(() => {
    if (!windowData) return
    const discovered = replaySymbols(windowData)
    setKnownSymbols(previous => rememberReplaySymbols(previous, windowData))
    if (!symbol && discovered[0]) setSymbol(discovered[0])
  }, [windowData, symbol])
  const requestedSeek = pendingSeek.current?.recording === recordingId && pendingSeek.current?.symbol === symbol ? pendingSeek.current.time_us : null
  const initialCursor = useMemo(() => range && requestedSeek != null && requestedSeek >= range.start_us && requestedSeek < range.end_us ? { time_us: requestedSeek } : { time_us: range?.start_us ?? 0, capture_seq: Number(windowData?.state_before_start?.state_through_seq ?? 0) }, [range, requestedSeek, windowData?.state_before_start])
  const currentCursor = useMemo(() => windowData && range && (cursorWindow !== windowData.window_id || requestedSeek != null) ? initialCursor : cursor, [windowData, range, cursorWindow, requestedSeek, initialCursor, cursor])
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
    if (range && (cursorWindow !== windowData.window_id || requestedSeek != null)) {
      setPlaying(false); setSelectedOrder(null); setSelectedTrade(null)
      setCursorWindow(windowData.window_id)
      setCursor(initialCursor)
      pendingSeek.current = null
      if (continuePlayback.current === windowData.recording_id) { continuePlayback.current = null; if (!document.hidden) setPlaying(true) }
    }
  }, [windowData, range, initialCursor, cursorWindow, requestedSeek])

  const pauseAndSeek = useCallback((timeUs: number) => {
    continuePlayback.current = null
    setPlaying(false)
    if (range) setCursor({ time_us: Math.max(range.start_us, Math.min(range.end_us - 1, Math.round(timeUs))) })
  }, [range])
  const seekRecording = (timeUs: number) => {
    continuePlayback.current = null
    setPlaying(false); setSeekDraft(null)
    if (!recording || !range) return
    const coverage = recording.coverage.find(item => timeUs >= item.start_us && timeUs < item.end_us)
    if (!coverage) return
    if (windowData && !loaded.error && !projected.error && timeUs >= range.start_us && timeUs < range.end_us) { setCursor({ time_us: timeUs }); return }
    pendingSeek.current = { recording: recordingId, symbol, time_us: timeUs }
    const duration = Number(windowDuration) * 1_000_000
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
    if (!playing || !windowData || !range || cursorWindow !== windowData.window_id || projected.error || document.hidden) return
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
          setRange({ start_us: range.end_us, end_us: Math.min(coverage.end_us, range.end_us + Number(windowDuration) * 1_000_000) })
        }
      }
      else frame = requestAnimationFrame(advance)
    }
    frame = requestAnimationFrame(advance)
    return () => cancelAnimationFrame(frame)
  }, [playing, speed, windowData, range, cursorWindow, projected.error, recording, windowDuration])

  const selectRecording = (value: string) => {
    continuePlayback.current = null
    setPlaying(false); setSeekDraft(null); pendingSeek.current = null; setRecordingId(value)
    const next = manifest?.recordings.find(item => item.recording_id === value)
    if (!next) return
    setSymbol(replaySymbols(next, knownSymbols[value] ?? [])[0] ?? ''); setRange(replayRange(next.coverage, undefined, Number(windowDuration) * 1_000_000))
  }
  const moveWindow = (direction: -1 | 1) => {
    continuePlayback.current = null
    if (!recording || !range) return
    setPlaying(false)
    const duration = Number(windowDuration) * 1_000_000
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
  const depth = projection ? currentReplayQuote(projection.quotes[symbol], projection.time_us, windowData?.gaps ?? []) : null
  const depthLabel = depth ? `已录买 ${depth.bids.length} / 卖 ${depth.asks.length} 档` : '盘口未知／已失效'
  const warnings = [...new Set([...(projection?.warnings ?? []), ...(windowData?.gaps ?? recording?.gaps ?? []).map(gap => gap.reason ?? '存在记录缺口')])]
  const canPrevWindow = Boolean(range && recording?.coverage.some(item => item.start_us < range.start_us))
  const canNextWindow = Boolean(range && recording?.coverage.some(item => item.end_us > range.end_us))
  const currentCoverage = recording?.coverage.find(item => range && range.start_us >= item.start_us && range.start_us < item.end_us)
  const readReady = projection != null && windowData != null && !loaded.loading
  const navigationTime = projection ? currentCursor.time_us : range?.start_us ?? 0
  const visibleOrders = Object.values(projection?.orders ?? {}).filter(order => order.symbol === symbol)
  const activeOrder = visibleOrders.find(order => order.key === selectedOrder)?.key ?? visibleOrders.find(order => replayOwnership(order, executionId).kind === 'current')?.key ?? visibleOrders[0]?.key ?? null
  const inspect = () => { continuePlayback.current = null; setPlaying(false) }
  const inspectOrder = (key: string) => { inspect(); setSelectedOrder(key); setSelectedTrade(null); setDetailTab('orders') }
  const inspectTrade = (key: string) => {
    inspect(); setSelectedTrade(key); setDetailTab('trades')
    const trade = projection?.ownTrades.find(value => replayTradeKey(value) === key)
    const order = visibleOrders.find(value => trade?.order_id != null && value.order_id === trade.order_id)
    if (order) setSelectedOrder(order.key)
  }
  const firstQuote = events.find(event => event.topic === `/quote/${symbol}` && event.data.book_valid === true)

  return <section className="replay-workbench" aria-label="执行回放">
    <header className="replay-desk-header">
      <div className="flex min-w-0 flex-wrap items-center gap-3"><AccountPageTitle accountId={accountId} page="执行回放" name={item?.name} channel={item?.trade_channel ?? context?.channel} market={item?.market} flip={false} back={{ to: `/accounts/${accountId}/executions/${executionId}`, label: '返回执行详情' }} /></div>
      <div className="replay-header-actions">
        <button type="button" popoverTarget="replay-information" className={buttonClass} aria-label="录制信息与完整性说明"><Info size={15} aria-hidden /><span>录制信息</span>{warnings.length > 0 && <span className="text-warn">{warnings.length}</span>}</button>
        <button type="button" onClick={() => setRevision(value => value + 1)} className={buttonClass} aria-label="重新读取已封口记录" disabled={manifestLoading || loaded.loading}><RefreshCw size={14} aria-hidden /></button>
      </div>
    </header>
    <div className="replay-read-errors" hidden={!manifestError && !loaded.error && !projected.error}>
      <ErrorNotice intent="read" title="回放目录读取失败" error={manifestError} onRetry={() => setRevision(value => value + 1)} />
      <ErrorNotice intent="read" title="回放窗口读取失败" error={loaded.error ?? projected.error} onRetry={() => setRevision(value => value + 1)} />
    </div>
    {manifestLoading && <p className="replay-empty" role="status">正在查找这次执行的录制…</p>}
    {manifest && !manifest.recordings.length && <div className="replay-empty"><h2 className="font-medium text-ink-1">{availabilityLabels[manifest.availability] ?? '暂无可回放记录'}</h2><p className="mt-2 max-w-xl text-sm text-ink-2">{manifest.reason ?? '这次执行没有已封口的录制。账户录制默认关闭，开启前的数据不会补录。'}</p></div>}
    {recording && context && <>
      {projection && windowData && !loaded.loading ? <OrdersAndTrades projection={projection} context={context} symbol={symbol} selected={activeOrder} onSelect={inspectOrder} selectedTradeKey={selectedTrade} onSelectTrade={inspectTrade} executionId={executionId} tab={detailTab} onTabChange={setDetailTab} onSeek={seekRecording} coverage={recording.coverage} gaps={windowData.gaps}>
      <div className="replay-desk-status">
        <span className="text-xs text-ink-2">{depthLabel} <span className="replay-clock-zone">· 上海时间</span></span>
        {firstQuote && navigationTime < firstQuote.time_us && <button type="button" className="text-xs text-accent underline-offset-4 hover:underline" onClick={() => pauseAndSeek(firstQuote.time_us)}>首个盘口</button>}
        <span className="replay-target num text-xs text-ink-2">目标 {replayNumber(target, unit?.quantity_decimals)} / 本次成交 {replayNumber(ownQuantity, unit?.quantity_decimals)} {unit?.quantity_label}</span>
      </div>
      <div className="replay-main-chart">
        {loaded.loading && <div className="replay-empty" role="status">正在恢复所选时刻的状态，回放已暂停…</div>}
        {projection && windowData && range && !loaded.loading && <ReplayChart window={windowData} range={range} projection={projection} symbol={symbol} selectedOrder={activeOrder} onSelectOrder={inspectOrder} selectedTrade={selectedTrade} onSelectTrade={inspectTrade} onInspect={inspect} onSeek={pauseAndSeek} executionId={executionId} symbolControl={symbols.length > 1 ? <Select ariaLabel="选择回放品种" searchable className={selectClass} value={symbol} options={symbols.map(value => ({ value, label: value }))} onChange={value => { continuePlayback.current = null; setPlaying(false); setSelectedOrder(null); setSelectedTrade(null); setSymbol(value) }} /> : <span className="text-[13px] font-semibold text-ink-1">{symbol || '无已录品种'}</span>} />}
        {!range && <p className="replay-empty">该录制段没有可读取的封口覆盖区间。</p>}
        {!loaded.loading && range && !readReady && <p className="replay-empty text-ink-2">所选窗口暂不可用。可在下方切换窗口或定位其他时段。</p>}
      </div>
      </OrdersAndTrades> : <div className="replay-empty" role="status">{loaded.loading ? '正在恢复所选时刻的状态，回放已暂停…' : '所选窗口暂不可用，可在录制信息中切换窗口。'}</div>}
      {range && <footer className="replay-transport" aria-label="回放播放控制" onKeyDown={event => { if (event.defaultPrevented || event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement || event.target instanceof Element && event.target.closest('[role="slider"], [role="combobox"], [role="listbox"], [aria-haspopup="listbox"]')) return; if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); if (readReady) step(event.key === 'ArrowLeft' ? -1 : 1) } }}>
        <div className="replay-transport-row">
          <div className="replay-play-controls">
            <button type="button" className={buttonClass} onClick={() => step(-1)} disabled={!readReady || eventIndex < 0} aria-label="前一观察"><SkipBack size={15} aria-hidden /><span>上一步</span></button>
            <button type="button" className={`${buttonClass} replay-play-button`} aria-label={playing ? '暂停回放' : '播放回放'} onClick={() => { continuePlayback.current = null; if (!playing && navigationTime >= range.end_us - 1) pauseAndSeek(range.start_us); setPlaying(value => !value) }} disabled={!readReady}>{playing ? <Pause size={15} aria-hidden /> : <Play size={15} aria-hidden />}<InkRewrite text={playing ? '暂停' : '播放'} tone="label" /></button>
            <button type="button" className={buttonClass} onClick={() => step(1)} disabled={!readReady || eventIndex >= events.length - 1} aria-label="后一观察"><SkipForward size={15} aria-hidden /><span>下一步</span></button>
            <label className="replay-speed-picker"><span>倍速</span><Select ariaLabel="播放速度" className={selectClass} value={speed} onChange={setSpeed} options={['0.5', '1', '2', '4'].map(value => ({ value, label: `${value}×` }))} /></label>
          </div>
          <time className="replay-cursor-time num text-xs text-ink-1">{replayTime(navigationTime)} <span className="text-ink-2">{playing ? '播放中' : '已暂停'}</span></time>

        </div>
        <div className="replay-timeline num">
          <p id="replay-seek-description" className="sr-only">{seekDraft == null ? '录制定位 · 同一微秒可逐事件查看' : `准备定位 ${replayTime(seekDraft, true)} · 松开后读取`}</p>
          <ValueSlider label="录制定位" describedBy="replay-seek-description" min={0} max={Math.max(1, (currentCoverage?.end_us ?? range.end_us) - (currentCoverage?.start_us ?? range.start_us) - 1)} step={1} value={Math.max(0, (seekDraft ?? navigationTime) - (currentCoverage?.start_us ?? range.start_us))}
            formatValue={offset => replayTime((currentCoverage?.start_us ?? range.start_us) + offset)}
            onChange={offset => { continuePlayback.current = null; setPlaying(false); setSeekDraft((currentCoverage?.start_us ?? range.start_us) + offset) }}
            onChangeEnd={offset => seekRecording((currentCoverage?.start_us ?? range.start_us) + offset)} />
        </div>
      </footer>}
    </>}
    <div id="replay-information" popover="auto" className="replay-information bg-surface text-ink-1" aria-label="录制信息与完整性说明">
      <div className="mb-4 flex items-center justify-between gap-4"><h2 className="text-sm font-medium">录制信息</h2><button type="button" popoverTarget="replay-information" popoverTargetAction="hide" className={buttonClass} aria-label="关闭录制信息"><X size={15} aria-hidden /></button></div>
      <p className="mb-3 break-all text-xs text-ink-2">账户 #{accountId} · 执行 {executionId}</p>
      {recording && context && <>
          <div className="replay-window-controls mb-4">
            <button type="button" className={buttonClass} onClick={() => moveWindow(-1)} disabled={!canPrevWindow} aria-label="上一窗口"><ChevronLeft size={14} aria-hidden /><span className="sr-only">上一窗口</span></button>
            <label><span>读取窗口 </span><Select value={windowDuration} ariaLabel="窗口时长" className={selectClass} onChange={value => { continuePlayback.current = null; setPlaying(false); setWindowDuration(value); setRange(replayRange(recording.coverage, range?.start_us, Number(value) * 1_000_000)) }} options={['2', '5', '10', '20'].map(value => ({ value, label: `${value} 秒` }))} /></label>
            <button type="button" className={buttonClass} onClick={() => moveWindow(1)} disabled={!canNextWindow} aria-label="下一窗口"><span className="sr-only">下一窗口</span><ChevronRight size={14} aria-hidden /></button>
          </div>
        <div className="mb-3 text-xs text-ink-2"><p className="mb-1">录制段</p><Select ariaLabel="选择录制段" className={`${selectClass} w-full`} value={recordingId} onChange={selectRecording} options={(manifest?.recordings ?? []).map((value, index) => ({ value: value.recording_id, label: `第 ${index + 1} 段${value.order_known === false ? '（先后未知）' : ''}` }))} /></div>
        <OverflowText text={recording.recording_id} className="mb-3 block text-xs text-ink-2" />
        {recording.coverage.length > 1 && <div className="mb-3 text-xs text-ink-2"><p className="mb-1">可用时段</p><Select ariaLabel="选择可用覆盖时段" className={`${selectClass} w-full`} value={currentCoverage?.start_us ?? null} onChange={value => { if (value == null) return; continuePlayback.current = null; setPlaying(false); setSeekDraft(null); pendingSeek.current = null; setRange(replayRange(recording.coverage, value, Number(windowDuration) * 1_000_000)) }} options={recording.coverage.map(value => ({ value: value.start_us, label: `${replayTime(value.start_us, true, false)} — ${replayTime(value.end_us, true, false)}` }))} /></div>}
        {recording.symbols_complete === false && <p className="mb-3 text-xs text-ink-2">品种目录尚不完整，后续窗口继续补充。</p>}
        <p className="mb-3 text-xs text-ink-2">{environmentLabels[context.environment ?? ''] ?? context.environment ?? '环境未知'} · {context.account_mode ?? '账户模式未知'} · {positionModeLabels[context.position_mode ?? ''] ?? '持仓模式未知'} · 上海时间</p>
        <div className="space-y-2 border-t border-line py-3 text-xs text-ink-2">{['depth', 'market_trade', 'own_order', 'own_trade', 'position'].map((name, index) => { const stream = replayStream(context, symbol, name); return <p key={name}>{['盘口', '市场成交', '自有委托', '自有成交', '持仓'][index]}：{stream ? availabilityLabels[stream.availability] : '未说明'}{stream?.reason ? ` · ${stream.reason === 'waiting_observation' ? '等待观察' : stream.reason}` : ''}{stream?.delivery ? ` · ${stream.delivery}` : ''}</p> })}</div>
        {warnings.length > 0 && <div className="mb-3 space-y-2 text-xs text-warn" role="status">{warnings.map(warning => <p key={warning}>{warning}</p>)}</div>}
        <p className="border-t border-line pt-3 text-xs text-ink-2">只读 · 仅呈现游标处已观察到的数据。热力颜色表示挂量，有限盘口不代表排队位置；完整性与缺失以本次录制说明为准。</p>
      </>}
    </div>
  </section>
}
