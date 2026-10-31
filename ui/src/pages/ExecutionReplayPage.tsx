import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router'
import { ChevronLeft, ChevronRight, Pause, Play, RefreshCw, SkipBack, SkipForward } from 'lucide-react'
import { BackLink } from '@/components/ui/BackLink'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Segmented } from '@/components/ui/Segmented'
import { getReplayManifest } from '@/lib/api/replay'
import { projectReplay } from '@/features/replay/model'
import { ReplayChart } from '@/features/replay/ReplayChart'
import { appliedEventIndex, playbackCursor } from '@/features/replay/playback'
import { replayOwnership } from '@/features/replay/ownership'
import { rememberReplaySymbols, replayCommandPresentation, replayFieldText, replayInstrument, replayNumber, replayRange, replayStream, replaySymbols, replayTime, replayWindowRequest } from '@/features/replay/presentation'
import { useReplayWindow } from '@/features/replay/useReplayWindow'
import type { ReplayContext, ReplayCursor, ReplayManifest, ReplayOrder, ReplayProjection, ReplayRange, ReplayTrade } from '@/features/replay/types'

const buttonClass = 'inline-flex min-h-10 items-center justify-center gap-1.5 rounded-chip border border-line px-3 text-[13px] text-ink-2 hover:bg-fill disabled:cursor-default disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
const selectClass = 'min-h-10 max-w-full rounded-chip border border-line bg-surface px-3 text-[13px] text-ink-1 focus-visible:outline-2 focus-visible:outline-accent'
const statusLabels: Record<string, string> = { pending: '请求中', working: '已受理', partially_filled: '部分成交', pending_cancel: '撤单待确认', filled: '已成交', canceled: '已撤销', rejected: '已拒绝', expired: '已过期', unknown: '状态未知', trigger_wait: '等待触发', activated: '已激活，待确认场内状态' }
const availabilityLabels: Record<string, string> = { available: '可用', partial: '部分覆盖', unavailable: '不可用', not_recorded: '未记录', unsupported: '暂不支持' }
const environmentLabels: Record<string, string> = { live: '实盘', demo: '演示', sim: '本地模拟', kq: '快期模拟' }
const positionModeLabels: Record<string, string> = { net: '净持仓', hedge: '双向持仓' }
const directionText = (value: string | null) => value === 'BUY' ? '买' : value === 'SELL' ? '卖' : '方向未知'
const offsetText = (value: string | null) => ({ OPEN: '开', CLOSE: '平', CLOSETODAY: '平今', CLOSEYESTERDAY: '平昨' } as Record<string, string>)[value ?? ''] ?? ''

function RecordedFields({ value, context, kind, executionId }: { value: ReplayOrder | ReplayTrade; context: ReplayContext; kind: 'order' | 'trade'; executionId: string }) {
  const ownership = replayOwnership(value, executionId)
  const fields = context.display?.[kind] ?? []
  const rows = fields.flatMap(field => { const text = replayFieldText(value.attributes[field.key], field); return text == null ? [] : [{ ...field, text }] })
  const identities = Object.entries(value.native_identity).filter(([, raw]) => typeof raw === 'string' || typeof raw === 'number')
  return <div className="mt-3 border-t border-line pt-3">
    <h3 className="text-[13px] font-semibold text-ink-1">{kind === 'order' ? '委托详情' : '成交详情'} · {value.order_id ?? '尚未关联委托'}</h3>
    <p className="mt-2 break-all text-[12px] text-ink-2">归属：{ownership.label}{ownership.executionId && <> · 执行 <span className="num select-text">{ownership.executionId}</span></>}</p>
    {'observed_status' in value && value.status === 'unknown' && value.observed_status && value.observed_status !== value.status && <p className="mt-2 text-[12px] text-warn">完整委托查询中未出现，当前状态未知；最近观察：{statusLabels[value.observed_status] ?? value.observed_status}。</p>}
    <dl className="mt-2 grid gap-x-8 gap-y-1 text-[12px] text-ink-2 sm:grid-cols-2">
      {rows.map(field => <div key={field.key} className="flex min-w-0 justify-between gap-3"><dt>{field.group ? `${field.group} · ` : ''}{field.label}</dt><dd className="num break-all text-right">{field.text}</dd></div>)}
    </dl>
    {!rows.length && <p className="mt-2 text-[12px] text-ink-3">本条记录没有额外的已声明字段。</p>}
    {identities.length > 0 && <details className="mt-3 text-[12px] text-ink-3"><summary className="w-fit cursor-pointer focus-visible:outline-2 focus-visible:outline-accent">原生身份</summary><dl className="mt-2 grid gap-2 sm:grid-cols-2">{identities.map(([key, raw]) => <div key={key}><dt>{key}</dt><dd className="num select-text break-all text-ink-2">{String(raw)}</dd></div>)}</dl></details>}
  </div>
}

function OrdersAndTrades({ projection, context, symbol, selected, onSelect, executionId }: { projection: ReplayProjection; context: ReplayContext; symbol: string; selected: string | null; onSelect: (key: string) => void; executionId: string }) {
  const instrument = replayInstrument(context, symbol), decimals = instrument.quantity_decimals ?? 6
  const orders = Object.values(projection.orders).filter(order => order.symbol === symbol)
  const selectedOrder = orders.find(order => order.key === selected)
  const [tradeKey, setTradeKey] = useState<string | null>(null)
  const trades = projection.ownTrades.filter(trade => trade.symbol === symbol)
  const selectedTrade = trades.find(trade => `${trade.capture_seq}:${trade.key}` === tradeKey)
  const commands = projection.commands.filter(command => !command.symbol || command.symbol === symbol).slice(-6)
  return <div className="mt-5 grid gap-x-8 gap-y-6 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
    <section className="min-w-0" aria-label="游标处自有委托">
      <div className="mb-3 flex items-baseline justify-between gap-3"><h2 className="font-semibold text-ink-1">自己的委托</h2><span className="text-[12px] text-ink-3">选择一张单查看轨迹</span></div>
      <div className="overflow-x-auto"><table className="num w-full text-left text-[12px]"><thead className="text-ink-3"><tr><th className="pb-2 font-medium">委托</th><th className="pb-2 font-medium">归属</th><th className="pb-2 font-medium">买卖 / 开平</th><th className="pb-2 text-right font-medium">价格</th><th className="pb-2 text-right font-medium">已成 / 委托</th><th className="pb-2 pl-3 font-medium">当时状态</th></tr></thead><tbody>
        {orders.map(order => <tr key={order.key} className={`border-t border-line ${order.key === selected ? 'bg-accent-soft' : ''}`}>
          <td className="py-2 pr-3"><button type="button" aria-pressed={order.key === selected} onClick={() => onSelect(order.key)} className="min-h-9 max-w-36 cursor-pointer truncate text-left text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-accent" title={order.order_id}>{order.order_id}</button></td>
          <td className="whitespace-nowrap pr-3 text-ink-2">{replayOwnership(order, executionId).label}</td>
          <td className="whitespace-nowrap">{directionText(order.direction)} {offsetText(order.offset)}</td><td className="pl-3 text-right">{replayNumber(order.price, instrument?.price_decimals)}</td><td className="whitespace-nowrap pl-3 text-right">{replayNumber(order.filled_volume, decimals)} / {replayNumber(order.volume, decimals)}</td><td className="pl-3"><span className={order.cancel_pending || !order.confirmed && !order.terminal ? 'text-warn' : ''}>{order.cancel_pending ? '撤单待确认' : statusLabels[order.status ?? 'unknown'] ?? '状态未知'}</span>{order.carry_in && <small className="block text-ink-3">窗口前已存在</small>}</td>
        </tr>)}
      </tbody></table></div>
      {!orders.length && <p className="py-4 text-[13px] text-ink-3">此时尚未观察到相关委托。</p>}
      {selectedOrder && <RecordedFields value={selectedOrder} context={context} kind="order" executionId={executionId} />}
      {commands.length > 0 && <details className="mt-3 border-t border-line pt-3 text-[12px] text-ink-3"><summary className="w-fit cursor-pointer focus-visible:outline-2 focus-visible:outline-accent">最近报撤请求 · {commands.length} 条观察</summary><ul className="mt-2 space-y-1.5">{commands.map(command => {
        const observation = replayCommandPresentation(command.data)
        return <li key={`${command.capture_seq}:${command.command_id}`} className="flex flex-wrap gap-x-3"><time className="num">{replayTime(command.time_us)}</time><span>{command.operation === 'cancel_order' ? '撤单' : command.operation === 'place_order' ? '报单' : '操作'} {command.order_id ?? command.symbol ?? ''} · {replayOwnership(command.data, executionId).label}</span><span className={observation.failed ? 'text-warn' : ''}>{observation.text}</span></li>
      })}</ul></details>}
    </section>
    <section className="min-w-0" aria-label="当前游标已知的自己的成交">
      <div className="mb-3 flex items-baseline justify-between gap-3"><h2 className="font-semibold text-ink-1">自己的成交</h2><span className="text-[12px] text-ink-3">独立于市场成交圆点</span></div>
      <div className="max-h-72 overflow-y-auto">{trades.map(trade => <button type="button" key={`${trade.capture_seq}:${trade.key}`} onClick={() => setTradeKey(`${trade.capture_seq}:${trade.key}`)} aria-pressed={`${trade.capture_seq}:${trade.key}` === tradeKey} className={`grid min-h-11 w-full grid-cols-[minmax(0,1fr)_auto] gap-x-3 border-t border-line py-2 text-left text-[12px] focus-visible:outline-2 focus-visible:outline-accent ${`${trade.capture_seq}:${trade.key}` === tradeKey ? 'bg-accent-soft' : 'hover:bg-fill'}`}><span className="num text-ink-3">{replayTime(trade.time_us)}<span className="block text-ink-2">{trade.order_id ?? '委托待关联'} · {directionText(trade.direction)} · {replayOwnership(trade, executionId).label}</span></span><span className="num text-right text-ink-1">{replayNumber(trade.quantity, decimals)} {instrument?.quantity_label} @ {replayNumber(trade.price, instrument?.price_decimals)}{trade.identity_quality !== 'stable' && <span className="block text-warn">身份未确认，不作为确定累计</span>}</span></button>)}</div>
      {!trades.length && <p className="py-4 text-[13px] text-ink-3">游标前暂无已记录的自有成交。</p>}
      {selectedTrade && <RecordedFields value={selectedTrade} context={context} kind="trade" executionId={executionId} />}
    </section>
  </div>
}

export function ExecutionReplayPage() {
  const { id, executionId = '' } = useParams(), accountId = Number(id)
  const [manifestValue, setManifest] = useState<ReplayManifest | null>(null), [manifestError, setManifestError] = useState<Error | null>(null)
  const manifest = manifestValue?.account_id === accountId && manifestValue.execution_id === executionId ? manifestValue : null
  const [manifestLoading, setManifestLoading] = useState(true), [revision, setRevision] = useState(0)
  const [recordingId, setRecordingId] = useState(''), [symbol, setSymbol] = useState(''), [range, setRange] = useState<ReplayRange | null>(null)
  const [knownSymbols, setKnownSymbols] = useState<Record<string, string[]>>({})
  const [cursor, setCursor] = useState<ReplayCursor>({ time_us: 0 }), [playing, setPlaying] = useState(false), [speed, setSpeed] = useState('1')
  const [cursorWindow, setCursorWindow] = useState('')
  const [selectedOrder, setSelectedOrder] = useState<string | null>(null), [windowDuration, setWindowDuration] = useState('20')
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
    if (!windowData) { setPlaying(false); setSelectedOrder(null); return }
    if (range && (cursorWindow !== windowData.window_id || requestedSeek != null)) {
      setPlaying(false); setSelectedOrder(null)
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
  const positions = projection ? Object.values(projection.observedPositions).filter(row => row.symbol === symbol) : []
  const derived = projection ? Object.values(projection.derivedPositions).filter(row => row.symbol === symbol) : []
  const target = projection?.targetsByExecution[executionId]?.[symbol]
  const ownQuantity = projection?.executionSymbolComplete[executionId]?.[symbol] ? projection.executionTotals[executionId]?.[symbol] ?? 0 : null
  const depth = projection?.quotes[symbol]
  const depthLabel = depth ? `已观察买 ${depth.bids.length} / 卖 ${depth.asks.length} 档` : '尚未观察盘口'
  const warnings = [...new Set([...(projection?.warnings ?? []), ...(windowData?.gaps ?? recording?.gaps ?? []).map(gap => gap.reason ?? '存在记录缺口')])]
  const canPrevWindow = Boolean(range && recording?.coverage.some(item => item.start_us < range.start_us))
  const canNextWindow = Boolean(range && recording?.coverage.some(item => item.end_us > range.end_us))
  const currentCoverage = recording?.coverage.find(item => range && range.start_us >= item.start_us && range.start_us < item.end_us)
  const readReady = projection != null && windowData != null && !loaded.loading
  const navigationTime = projection ? currentCursor.time_us : range?.start_us ?? 0

  return <section className="min-w-0" aria-label="执行回放">
    <div className="flex flex-wrap items-start justify-between gap-3"><div className="flex min-w-0 items-center gap-2"><BackLink to={`/accounts/${accountId}/executions/${executionId}`} label="返回执行详情" /><div className="min-w-0"><h1 className="text-xl font-semibold text-ink-1">执行回放</h1><p className="mt-1 truncate text-[12px] text-ink-2">账户 #{accountId} · {executionId}</p></div></div><button type="button" onClick={() => setRevision(value => value + 1)} className={buttonClass} disabled={manifestLoading || loaded.loading}><RefreshCw size={14} />重新读取已封口记录</button></div>
    <ErrorNotice intent="read" title="回放目录读取失败" error={manifestError} onRetry={() => setRevision(value => value + 1)} />
    {manifestLoading && <p className="py-12 text-[14px] text-ink-2" role="status">正在查找这次执行的录制…</p>}
    {manifest && !manifest.recordings.length && <div className="mt-6 border-t border-line py-10"><h2 className="font-semibold text-ink-1">{availabilityLabels[manifest.availability] ?? '暂无可回放记录'}</h2><p className="mt-2 max-w-2xl text-[14px] leading-6 text-ink-2">{manifest.reason ?? '这次执行没有已封口的录制。账户录制默认关闭，开启前的数据不会补录。'}</p></div>}
    {recording && context && <div className="mt-5 rounded-card border border-line bg-surface p-3 sm:p-5">
      <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
        <label className="min-w-0 text-[12px] text-ink-2">录制段<select aria-label="选择录制段" className={`${selectClass} mt-1 block max-w-72`} value={recordingId} onChange={event => selectRecording(event.target.value)}>{manifest?.recordings.map((item, index) => <option key={item.recording_id} value={item.recording_id}>第 {index + 1} 段 · {item.recording_id}{item.order_known === false ? '（先后未知）' : ''}</option>)}</select></label>
        <label className="text-[12px] text-ink-2">品种<select aria-label="选择回放品种" className={`${selectClass} mt-1 block`} value={symbol} onChange={event => { continuePlayback.current = null; setPlaying(false); setSelectedOrder(null); setSymbol(event.target.value) }}>{symbols.map(item => <option key={item} value={item}>{item}</option>)}</select>{recording.symbols_complete === false && <span className="mt-1 block max-w-64 text-[11px] leading-4 text-ink-3">品种目录尚不完整，后续窗口继续补充。</span>}</label>
        {recording.coverage.length > 1 && <label className="text-[12px] text-ink-2">可用时段<select aria-label="选择可用覆盖时段" className={`${selectClass} mt-1 block`} value={currentCoverage?.start_us ?? ''} onChange={event => { continuePlayback.current = null; setPlaying(false); setSeekDraft(null); pendingSeek.current = null; setRange(replayRange(recording.coverage, Number(event.target.value), Number(windowDuration) * 1_000_000)) }}>{recording.coverage.map(item => <option key={item.start_us} value={item.start_us}>{replayTime(item.start_us, true, false)} — {replayTime(item.end_us, true, false)}</option>)}</select></label>}
        <div className="min-w-0 flex-1 text-[12px] leading-5 text-ink-2"><p>{context.channel ?? '渠道未知'} · {environmentLabels[context.environment ?? ''] ?? context.environment ?? '环境未知'} · {context.account_mode ?? '账户模式未知'} · {positionModeLabels[context.position_mode ?? ''] ?? '持仓模式未知'}</p><p>{depthLabel} · {replayStream(context, symbol, 'depth')?.delivery ?? '交付粒度未知'} · 上海时间</p></div>
        <div className="text-right"><p className="text-[12px] text-ink-3">阶段目标 / 本次已确认成交</p><p className="num mt-1 text-[16px] text-ink-1">{replayNumber(target, unit?.quantity_decimals)} / {replayNumber(ownQuantity, unit?.quantity_decimals)} <span className="text-[12px] text-ink-2">{unit?.quantity_label}</span></p></div>
      </div>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-ink-2">{['depth', 'market_trade', 'own_order', 'own_trade', 'position'].map((name, index) => { const stream = replayStream(context, symbol, name); return <span key={name} title={stream?.reason}>{['盘口', '市场成交', '自有委托', '自有成交', '持仓'][index]}：{stream ? availabilityLabels[stream.availability] : '未说明'}{stream?.reason ? ` · ${stream.reason}` : ''}</span> })}</div>
      <ErrorNotice intent="read" title="回放窗口读取失败" error={loaded.error ?? projected.error} onRetry={() => setRevision(value => value + 1)} />
      {loaded.loading && <div className="grid min-h-80 place-items-center text-[14px] text-ink-2" role="status">正在恢复所选时刻的状态，回放已暂停…</div>}
      {projection && windowData && range && !loaded.loading && <ReplayChart window={windowData} range={range} projection={projection} symbol={symbol} selectedOrder={selectedOrder} onSelectOrder={setSelectedOrder} onSeek={pauseAndSeek} onStep={step} executionId={executionId} />}
      {range && <>
        <div className="mt-4 flex flex-wrap items-center gap-2" onKeyDown={event => { if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return; if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); step(event.key === 'ArrowLeft' ? -1 : 1) } }}>
          <button type="button" className={buttonClass} onClick={() => step(-1)} disabled={!readReady || eventIndex < 0}><SkipBack size={14} />前一观察</button>
          <button type="button" className={`${buttonClass} min-w-24 bg-accent text-surface hover:bg-accent`} disabled={!readReady} onClick={() => { if (navigationTime >= range.end_us - 1) pauseAndSeek(range.start_us); setPlaying(value => !value) }} aria-label={playing ? '暂停回放' : '播放回放'}>{playing ? <Pause size={15} /> : <Play size={15} />}{playing ? '暂停' : '播放'}</button>
          <button type="button" className={buttonClass} onClick={() => step(1)} disabled={!readReady || eventIndex >= events.length - 1}><SkipForward size={14} />后一观察</button>
          <Segmented value={speed} size="sm" options={['0.5', '1', '2', '4'].map(value => ({ value, label: `${value}×` }))} onChange={setSpeed} />
          <p className="num ml-auto text-[12px] text-ink-2"><time>{replayTime(navigationTime, true)}</time><span className="ml-3 text-ink-3">{projection ? `观察 #${projection.capture_seq}` : '状态尚未恢复'}</span></p>
        </div>
        <label className="mt-3 block text-[12px] text-ink-2" htmlFor="replay-time">{seekDraft == null ? '录制定位 · 同一微秒可逐事件查看' : `准备定位 ${replayTime(seekDraft, true)} · 松开后读取`}</label>
        <input id="replay-time" type="range" min={0} max={Math.max(1, (currentCoverage?.end_us ?? range.end_us) - (currentCoverage?.start_us ?? range.start_us) - 1)} step={1} value={Math.max(0, (seekDraft ?? navigationTime) - (currentCoverage?.start_us ?? range.start_us))} onChange={event => { continuePlayback.current = null; setPlaying(false); setSeekDraft((currentCoverage?.start_us ?? range.start_us) + Number(event.target.value)) }} onPointerUp={() => { if (seekDraft != null) seekRecording(seekDraft) }} onKeyUp={() => { if (seekDraft != null) seekRecording(seekDraft) }} onBlur={() => { if (seekDraft != null) seekRecording(seekDraft) }} aria-valuetext={`${replayTime(seekDraft ?? navigationTime, true)}${projection ? `，观察 ${projection.capture_seq}` : '，状态尚未恢复'}`} className="mt-1 min-h-8 w-full cursor-pointer accent-accent focus-visible:outline-2 focus-visible:outline-accent" />
        <div className="flex flex-wrap items-center justify-between gap-2 text-[12px] text-ink-3"><span className="num">{replayTime(range.start_us, true, false)} — {replayTime(range.end_us, true, false)}</span><div className="flex items-center gap-2"><button type="button" className={buttonClass} onClick={() => moveWindow(-1)} disabled={!canPrevWindow}><ChevronLeft size={13} />上一窗口</button><label>窗口 <select value={windowDuration} aria-label="窗口时长" className={selectClass} onChange={event => { continuePlayback.current = null; setPlaying(false); setWindowDuration(event.target.value); setRange(replayRange(recording.coverage, range.start_us, Number(event.target.value) * 1_000_000)) }}>{['2', '5', '10', '20'].map(value => <option key={value} value={value}>{value} 秒</option>)}</select></label><button type="button" className={buttonClass} onClick={() => moveWindow(1)} disabled={!canNextWindow}>下一窗口<ChevronRight size={13} /></button></div></div>
      </>}
      {projection && windowData && !loaded.loading && <>
        {warnings.length > 0 && <div className="mt-3 border-t border-line pt-3 text-[12px] leading-5 text-warn" role="status">{warnings.map(warning => <p key={warning}>{warning}</p>)}</div>}
        <section className="mt-4 border-y border-line py-3" aria-label="游标处持仓观察">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2"><h2 className="text-[13px] font-semibold text-ink-1">持仓观察</h2><p className="text-[11px] text-ink-2">推导需要业务对齐证据；可卖与冻结只采用原生观察。</p></div>
          {positions.length ? <div className="overflow-x-auto"><table className="num w-full text-right text-[12px]"><thead className="text-ink-3"><tr><th className="pb-1 text-left font-medium">方向</th>{['原生持仓', '今仓', '昨仓', '可卖 / 可平', '冻结', '有证据的推导'].map(label => <th key={label} className="whitespace-nowrap pb-1 pl-3 font-medium">{label}</th>)}</tr></thead><tbody>{positions.map(position => <tr key={position.direction ?? 'unknown'} className="border-t border-line text-ink-2"><th className="py-2 text-left font-normal">{({ LONG: '多头', SHORT: '空头', NET: '净持仓' } as Record<string, string>)[position.direction ?? ''] ?? '方向未知'}</th>{[position.volume, position.today_volume, position.yesterday_volume, position.available_volume, position.frozen_volume, derived.find(value => value.direction === position.direction)?.volume].map((value, index) => <td key={index} className="whitespace-nowrap pl-3">{replayNumber(value, unit?.quantity_decimals)}</td>)}</tr>)}</tbody></table></div> : <p className="text-[12px] text-ink-2">当前持仓尚未观察到，或该层不可用。</p>}
        </section>
        <OrdersAndTrades projection={projection} context={context} symbol={symbol} selected={selectedOrder} onSelect={setSelectedOrder} executionId={executionId} />
      </>}
      {!range && <p className="py-8 text-[14px] text-ink-2">该录制段没有可读取的封口覆盖区间。</p>}
      <p className="mt-5 border-t border-line pt-3 text-[11px] leading-5 text-ink-3">只读 · 仅呈现游标处已观察到的数据。热力颜色表示挂量，有限盘口不代表排队位置；完整性与缺失以本次录制说明为准。</p>
    </div>}
  </section>
}
