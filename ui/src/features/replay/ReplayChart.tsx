import { ResizeHandle } from '@/components/ui/ResizeHandle'
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { ArrowLeft, ArrowRight, Check, Circle, Crosshair, Info, Minus, RotateCcw, X } from 'lucide-react'
import { ValueSlider } from '@/components/ui/ValueSlider'
import { Tooltip } from '@/components/ui/Tooltip'
import { canvasLine, prepareCanvas, readCanvasTheme, type CanvasTheme } from '@/components/viz/chartCanvas'
import { centerReplayPrice, currentReplayQuote, replayChartLayout, replayMarketPriceSegments, replayOnBookOrder, replayPriceTicks, replayPriceY, replayQuoteEnd, replayRemainingQuantity, replayTimeX, replayXTime, replayYPrice, zoomReplayPrice, type ReplayChartLayout, type ReplayPriceView } from '@/features/replay/chartGeometry'
import { clampReplayView, replayOrderFocus, replayOrderTimeline, replayTradeKey, replayVisibleOrderMarks, replayVisibleOrders, zoomReplayPriceAt, zoomReplayTime } from '@/features/replay/chartInteraction'
import { replayBubbleRadius, replayMarketBubbleGroups, replayMarketBubblesAt } from '@/features/replay/marketBubbles'
import { useReplayHeat } from '@/features/replay/useReplayHeat'
import { replaySeekScheduler, replayTimeUpperBound } from '@/features/replay/chartLookup'
import { recordedDepth, replayBestLevel, replayObservedPriceView } from '@/features/replay/depthGeometry'
import { inspectReplayOrder, inspectReplayTrade, nearestReplayInspection, replayFutureRecord, type InspectionRecord } from '@/features/replay/inspection'
import { ReplayInspector } from '@/features/replay/ReplayInspector'
import { ReplayDepthLadder } from '@/features/replay/ReplayDepthLadder'
import { replayOrderOwnershipGroups, replayOwnership } from '@/features/replay/ownership'
import { replayAxisPrice, replayInstrument, replayNumber, replayTime } from '@/features/replay/presentation'
import { replayTimeLabelLeft, replayTimeTickOpacity, replayTimeTicks } from '@/features/replay/timeAxis'
import type { ReplayExecutionSlice } from '@/features/replay/executionScene'
import type { ReplayCursor, ReplayOrder, ReplayProjection, ReplayRange, ReplayWindow } from '@/features/replay/types'

interface Props {
  initialTimeUs?: number
  children?: ReactNode
  scene?: ReplayProjection
  slices?: ReplayExecutionSlice[]
  playControls?: ReactNode
  fullscreenControl?: ReactNode
  firstQuoteControl?: ReactNode
  symbolControl?: ReactNode
  window: ReplayWindow
  range: ReplayRange
  projection: ReplayProjection
  symbol: string
  executionId: string
  selectedOrder: string | null
  onSelectOrder: (key: string) => void
  onSeek: (timeUs: number, captureSeq?: number) => void
  selectedTrade: string | null
  onSelectTrade: (key: string) => void
  onInspect: () => void
}

const emptyBubbles: ReturnType<typeof replayMarketBubblesAt> = []

const controlClass = 'inline-flex min-h-7 min-w-7 cursor-pointer items-center justify-center rounded-chip text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-40'

function priceLabelOpacity(top: number, occupied: number[]) {
  // 同槽只隐藏真正相交的 16px 标签，不让附近刻度渐隐或移位。
  return occupied.some(other => Math.abs(top - other) < 16) ? 0 : 1
}

function drawOrderHistory(ctx: CanvasRenderingContext2D, timeline: ReturnType<typeof replayOrderTimeline>, range: ReplayRange, view: ReplayPriceView, layout: ReplayChartLayout, selected: string | null, executionId: string, theme: CanvasTheme, orderColor: string, cursor: ReplayCursor, hovered: string | undefined = undefined) {
  const { segments, marks } = timeline
  const color = (order: ReplayOrder) => replayFutureRecord(order.time_us, order.capture_seq, cursor) ? theme.ink : order.cancel_pending ? theme.warn : orderColor
  for (const { order, start, end, next } of segments) {
    if (order.price == null) continue
    const x = replayTimeX(start, range, layout), right = replayTimeX(end, range, layout), y = replayPriceY(order.price, view, layout)
    ctx.strokeStyle = color(order); ctx.lineWidth = order.key === selected ? 2 : 1.3
    ctx.globalAlpha = selected && order.key !== selected ? 0.45 : 1
    ctx.setLineDash(order.cancel_pending ? [5, 3] : replayOwnership(order, executionId).kind === 'current' ? [] : [2, 3])
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(Math.max(x + 1, right), y); ctx.stroke(); ctx.setLineDash([])
    if (next && replayOnBookOrder(next) && next.price != null && next.price !== order.price && next.time_us === end) {
      ctx.beginPath(); ctx.moveTo(right, y); ctx.lineTo(right, replayPriceY(next.price, view, layout)); ctx.stroke()
    }
  }
  for (const { order, kind, time, price } of marks) {
    const x = replayTimeX(time, range, layout), y = replayPriceY(price, view, layout), r = hovered === `order:${order.key}` ? 5 : 3.5
    ctx.globalAlpha = selected && order.key !== selected ? 0.45 : 1
    ctx.save(); ctx.shadowColor = theme.bg; ctx.shadowBlur = 3; ctx.shadowOffsetY = 2
    ctx.fillStyle = color(order); ctx.strokeStyle = theme.surface; ctx.lineWidth = 1.25; ctx.beginPath()
    if (kind === 'placed') { ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill() }
    else if (kind === 'filled' && (order.direction === 'BUY' || order.direction === 'SELL')) {
      const direction = order.direction === 'BUY' ? -1 : 1
      ctx.moveTo(x, y + direction * r); ctx.lineTo(x - r, y - direction * r); ctx.lineTo(x + r, y - direction * r); ctx.closePath(); ctx.fill()
    } else if (kind === 'canceled') { ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fillStyle = theme.surface; ctx.fill(); ctx.strokeStyle = color(order); ctx.lineWidth = 1.5 }
    else if (kind !== 'changed') ctx.fillText(kind === 'filled' ? '全成' : kind === 'expired' ? '过期' : '拒绝', x + 5, y - 7)
    ctx.shadowBlur = 0; ctx.stroke(); ctx.restore()
  }
  ctx.globalAlpha = 1
  ctx.lineWidth = 1
}

/** 同一画布中的历史深度、COB 与 ORD 只共享一份价位映射。 */
export function ReplayChart({ window: replayWindow, range, projection, symbol, executionId, selectedOrder, onSelectOrder, onSeek, selectedTrade, onSelectTrade, onInspect, playControls, fullscreenControl, firstQuoteControl, symbolControl, children, scene = projection, slices, initialTimeUs = range.start_us }: Props) {
  const holder = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null), hoverCanvas = useRef<HTMLCanvasElement>(null)
  const historyLayers = useRef<HTMLCanvasElement[]>([]), orderLayers = useRef<HTMLCanvasElement[]>([])
  const seekCallback = useRef(onSeek), sliderDragging = useRef(false)
  useLayoutEffect(() => { seekCallback.current = onSeek }, [onSeek])
  const seekScheduler = useMemo(() => replaySeekScheduler(time => seekCallback.current(time), callback => window.requestAnimationFrame(callback), frame => window.cancelAnimationFrame(frame)), [])
  const [size, setSize] = useState({ width: 900, height: 300, revision: 0 })
  const [depthWidth, setDepthWidth] = useState(346)
  const [resizingDepth, setResizingDepth] = useState(false)
  useLayoutEffect(() => {
    const stored = window.localStorage?.getItem('axon.replay.depthWidth')
    const value = Number(stored)
    if (stored != null && Number.isFinite(value) && value >= 346) setDepthWidth(value)
  }, [])
  const [manualView, setManualView] = useState<ReplayPriceView | null>(null)
  const [timePreview, setTimePreview] = useState<ReplayRange | null>(null)
  const [timeView, setTimeView] = useState<ReplayRange | null>(null)
  const viewScheduler = useMemo(() => replaySeekScheduler((next: { range?: ReplayRange; view?: ReplayPriceView }) => {
    if (next.range) setTimeView(next.range)
    if (next.view) setManualView(next.view)
  }, callback => window.requestAnimationFrame(callback), frame => window.cancelAnimationFrame(frame)), [])
  useLayoutEffect(() => () => viewScheduler.cancel(), [viewScheduler, replayWindow.window_id, symbol])
  const contentRange = useMemo(() => ({ ...range, start_us: Math.max(range.start_us, Math.min(range.end_us - 1, initialTimeUs)) }), [range, initialTimeUs])
  const visibleRange = timeView ?? contentRange
  const historyRange = timePreview ?? visibleRange
  const [layers, setLayers] = useState({ working: true, canceled: true, filled: true, market: true, execution: true })
  const recorded = scene
  const drawThrough = recorded.time_us
  const cursorInside = projection.time_us >= visibleRange.start_us && projection.time_us < visibleRange.end_us
  const [hover, setHover] = useState<(InspectionRecord & { cursor: string; x: number; y: number; records: InspectionRecord[];  }) | null>(null)
  const [inspection, setInspection] = useState<{ record: InspectionRecord; records: InspectionRecord[]; x: number; y: number } | null>(null)
  const gesture = useRef<{ id: number; x: number; y: number; view: ReplayPriceView; range: ReplayRange; mode: 'chart' | 'price' | 'time' | 'seek'; moved: boolean } | null>(null)
  useLayoutEffect(() => () => { seekScheduler.cancel(); sliderDragging.current = false; gesture.current = null }, [seekScheduler, replayWindow.recording_id, replayWindow.window_id, symbol, range.start_us, range.end_us])
  const instrument = replayInstrument(replayWindow.context, symbol)
  const orders = useMemo(() => Object.values(projection.orders).filter(order => order.symbol === symbol), [projection.orders, symbol])
  const selected = Object.values(scene.orders).find(order => order.key === selectedOrder)
  const currentQuote = useMemo(() => currentReplayQuote(projection.quotes[symbol], projection.time_us, replayWindow.gaps), [projection.quotes, symbol, projection.time_us, replayWindow.gaps])
  const historyQuotes = useMemo(() => recorded.quoteHistory.filter(quote => quote.symbol === symbol && quote.time_us <= drawThrough), [recorded.quoteHistory, symbol, drawThrough])
  const historyOrders = useMemo(() => recorded.orderHistory.filter(order => order.symbol === symbol && order.time_us <= drawThrough), [recorded.orderHistory, symbol, drawThrough])
  const firstQuote = useMemo(() => historyQuotes.find(quote => currentReplayQuote(quote, quote.time_us, replayWindow.gaps)), [historyQuotes, replayWindow.gaps])
  const futureDepth = Boolean(firstQuote && replayFutureRecord(firstQuote.time_us, firstQuote.capture_seq, projection))
  const displayQuote = currentQuote ?? (futureDepth ? firstQuote! : null)
  const visibleOrders = useMemo(() => replayVisibleOrders(historyOrders, layers), [historyOrders, layers])

  const initialView = useMemo(() => replayObservedPriceView(historyQuotes, historyOrders, recorded.marketTrades.filter(trade => trade.symbol === symbol), instrument?.price_tick), [historyQuotes, historyOrders, recorded.marketTrades, symbol, instrument?.price_tick])
  const view = manualView ?? initialView
  const hoverCursor = `${replayWindow.window_id}:${symbol}:${projection.time_us}:${projection.capture_seq}:${visibleRange.start_us}:${visibleRange.end_us}:${view?.min}:${view?.max}`
  const activeHover = hover?.cursor === hoverCursor ? hover : null
  const hoveredObject = activeHover?.object
  const selectedExecution = layers.execution && replayWindow.context.streams?.[symbol]?.own_trade?.availability !== 'unavailable' ? recorded.ownTrades.find(trade => replayTradeKey(trade) === selectedTrade && trade.symbol === symbol && trade.time_us >= visibleRange.start_us && trade.time_us < visibleRange.end_us && (!historyOrders.some(order => order.order_id === trade.order_id) || visibleOrders.some(order => order.order_id === trade.order_id))) : undefined
  const height = size.height
  const [priceAxisWidth, setPriceAxisWidth] = useState(40)
  const [axisInteracting, setAxisInteracting] = useState(false)
  const [pricePreview, setPricePreview] = useState<ReplayPriceView | null>(null)
  const historyView = axisInteracting && pricePreview ? pricePreview : view
  const wheelTimer = useRef<number | null>(null)
  const [timeCharacterWidth, setTimeCharacterWidth] = useState(6.6)
  const layout = useMemo(() => ({ ...replayChartLayout(size.width, height, priceAxisWidth, depthWidth), top: selectedExecution ? 28 : 0 }), [size.width, height, priceAxisWidth, depthWidth, selectedExecution])
  const maxDepthWidth = Math.max(346, size.width - priceAxisWidth - 240)
  const clampDepthWidth = (value: number) => Math.min(maxDepthWidth, Math.max(346, value))
  const saveDepthWidth = (value: number) => {
    const next = clampDepthWidth(value)
    setDepthWidth(next)
    window.localStorage.setItem('axon.replay.depthWidth', String(next))
  }
  const axisY = layout.bottom + 12
  const cursorX = replayTimeX(projection.time_us, visibleRange, layout)
  const cursorWidth = replayTime(projection.time_us).length * timeCharacterWidth + (cursorInside ? 8 : 24)
  const cursorLeft = replayTimeLabelLeft(cursorX, cursorWidth, layout)
  const timeTicks = useMemo(() => replayTimeTicks(visibleRange, layout, timeCharacterWidth), [visibleRange, layout, timeCharacterWidth])
  const inspectionWidth = Math.min(320, layout.right - 16)
  const priceTicks = useMemo(() => view ? replayPriceTicks(view, layout.bottom - layout.top) : [], [view, layout.bottom, layout.top])
  const priceLabelTop = (y: number) => Math.max(layout.top, Math.min(layout.bottom - 16, y - 8))
  const hoverPriceTop = activeHover ? priceLabelTop(activeHover.y) : null
  const priceLabelStyle = { left: layout.right, width: priceAxisWidth }
  const marketBubbleGroups = useMemo(() => {
    if (!layers.market) return []
    return slices ? slices.map(slice => slice.window.context.streams?.[symbol]?.market_trade?.availability === 'unavailable' ? [] : replayMarketBubbleGroups(slice.scene.marketTrades, symbol, historyRange, layout, slice.window.gaps))
      : [replayWindow.context.streams?.[symbol]?.market_trade?.availability === 'unavailable' ? [] : replayMarketBubbleGroups(recorded.marketTrades, symbol, historyRange, layout, replayWindow.gaps)]
  }, [layers.market, slices, replayWindow, recorded.marketTrades, symbol, historyRange, layout])
  const marketBubbles = useMemo(() => marketBubbleGroups.every(groups => groups.length === 0) ? emptyBubbles : marketBubbleGroups.flatMap(groups => replayMarketBubblesAt(groups, { time_us: projection.time_us, capture_seq: projection.capture_seq })), [marketBubbleGroups, projection.time_us, projection.capture_seq])
  const bubbles = useMemo(() => marketBubbles.filter(bubble => !historyView || bubble.price >= historyView.min && bubble.price <= historyView.max), [marketBubbles, historyView])
  const bubbleCursor = bubbles.length ? projection : null
  const maxBubble = bubbles.reduce((max, bubble) => Math.max(max, bubble.quantity), 1)
  const ownOrders = useMemo(() => orders.filter(replayOnBookOrder), [orders])
  const depthOrders = useMemo(() => futureDepth ? [] : ownOrders, [futureDepth, ownOrders])
  const decimals = instrument?.price_decimals ?? 6
  const priceText = useCallback((price: number | null) => replayNumber(price, decimals), [decimals])
  const axisDecimals = useMemo(() => {
    if (instrument.price_decimals != null || (instrument.price_tick != null && instrument.price_tick > 0)) return instrument.price_decimals
    // 旧录制缺精度元数据时，整段共用已录价格的最大小数位，不逐刻度裁掉尾零。
    const prices = [...historyQuotes.flatMap(quote => [...quote.bids, ...quote.asks].map(level => level.price)), ...historyOrders.map(order => order.price), ...recorded.marketTrades.filter(trade => trade.symbol === symbol).map(trade => trade.price)]
    return prices.reduce<number | undefined>((digits, price) => price == null || !Number.isFinite(price) ? digits : Math.max(digits ?? 0, price.toLocaleString('en-US', { useGrouping: false, maximumFractionDigits: 12 }).split('.')[1]?.length ?? 0), undefined)
  }, [instrument.price_decimals, instrument.price_tick, historyQuotes, historyOrders, recorded.marketTrades, symbol])
  const axisPriceText = useCallback((price: number | null) => replayAxisPrice(price, { price_decimals: axisDecimals, price_tick: instrument.price_tick }), [axisDecimals, instrument.price_tick])
  const quantityText = useCallback((quantity: number | null) => replayNumber(quantity, instrument?.quantity_decimals ?? 6), [instrument?.quantity_decimals])
  const priceTick = instrument.price_tick ?? 10 ** -(axisDecimals ?? decimals)
  const hoverPrice = activeHover && view ? Math.round(replayYPrice(activeHover.y, view, layout) / priceTick) * priceTick : null
  const orderPrices = useMemo(() => [...new Set(depthOrders.flatMap(order => order.price != null ? [order.price] : []))], [depthOrders])
  const orderLabels: { price: number; top: number; y: number; pending: boolean; collapsed: boolean }[] = []
  for (const price of [...orderPrices].sort((a, b) => Number(b === selected?.price) - Number(a === selected?.price) || b - a)) {
    if (!view || price < view.min || price > view.max) continue
    const y = replayPriceY(price, view, layout), top = priceLabelTop(y)
    orderLabels.push({ price, y, top, pending: depthOrders.some(order => order.price === price && order.cancel_pending),
      collapsed: orderLabels.some(label => !label.collapsed && Math.abs(label.top - top) < 20) })
  }
  const hoverOccupied = hoverPriceTop == null ? [] : [hoverPriceTop]
  const hoverOrder = orderLabels.find(label => axisPriceText(label.price) === axisPriceText(hoverPrice))
  const occupiedPrices = [...hoverOccupied, ...orderLabels.filter(label => !label.collapsed).map(label => label.top)]
  const heatSources = useMemo(() => slices
    ? slices.map(slice => ({ quotes: slice.scene.quoteHistory.filter(quote => quote.symbol === symbol), gaps: slice.window.gaps, end: Math.min(drawThrough, slice.range.end_us - 1) }))
    : [{ quotes: historyQuotes, gaps: replayWindow.gaps, end: drawThrough }], [slices, symbol, historyQuotes, replayWindow.gaps, drawThrough])
  const heatQuery = useMemo(() => historyView ? { range: historyRange, view: historyView, layout } : null, [historyRange, historyView, layout])
  const { frame: heatFrame, error: heatError, pending: heatPending } = useReplayHeat(heatSources, heatQuery)
  const heat = heatFrame?.value ?? null
  const heatRange = heatFrame?.query.range ?? historyRange
  const heatView = heatFrame?.query.view ?? historyView
  const maxHeat = heat?.max ?? 0
  const marketSegments = useMemo(() => slices ? slices.flatMap(slice => slice.window.context.streams?.[symbol]?.market_trade?.availability === 'unavailable' ? [] : replayMarketPriceSegments(slice.scene.marketTrades, symbol, heatRange, Math.min(drawThrough, slice.range.end_us - 1), slice.window.gaps)) : replayWindow.context.streams?.[symbol]?.market_trade?.availability === 'unavailable' ? [] : replayMarketPriceSegments(recorded.marketTrades, symbol, heatRange, drawThrough, replayWindow.gaps), [slices, replayWindow, recorded.marketTrades, drawThrough, symbol, heatRange])
  const timeline = useMemo(() => {
    if (!slices) return replayOrderTimeline(visibleOrders, symbol, range, recorded)
    const visible = new Set(visibleOrders)
    const timelines = slices.flatMap(slice => {
      const bounds = { start_us: Math.max(range.start_us, slice.range.start_us), end_us: Math.min(range.end_us, slice.range.end_us) }
      return bounds.start_us < bounds.end_us ? [replayOrderTimeline(slice.scene.orderHistory.filter(order => visible.has(order)), symbol, bounds, slice.scene)] : []
    })
    return { segments: timelines.flatMap(value => value.segments), marks: timelines.flatMap(value => value.marks) }
  }, [slices, visibleOrders, symbol, range, recorded])
  const visibleTimeline = useMemo(() => historyView ? { ...timeline, marks: replayVisibleOrderMarks(timeline.marks.filter(mark => mark.time >= historyRange.start_us && mark.time < historyRange.end_us), historyRange, historyView, layout) } : timeline, [timeline, historyRange, historyView, layout])
  const orderHighlights = useMemo(() => {
    const byObject = new Map<string, typeof visibleTimeline>(), byTime = new Map<number, typeof visibleTimeline>()
    for (const mark of visibleTimeline.marks) {
      const object = `order:${mark.order.key}`, row = byObject.get(object) ?? { segments: [], marks: [] }
      row.marks.push(mark); byObject.set(object, row)
      const atTime = byTime.get(mark.order.time_us) ?? { segments: [], marks: [] }
      atTime.marks.push(mark); byTime.set(mark.order.time_us, atTime)
    }
    for (const segment of visibleTimeline.segments) {
      const row = byTime.get(segment.order.time_us) ?? { segments: [], marks: [] }
      row.segments.push(segment); byTime.set(segment.order.time_us, row)
    }
    return { byObject, byTime }
  }, [visibleTimeline])

  useLayoutEffect(() => {
    setManualView(null); setTimeView(null); setHover(null); setInspection(null); gesture.current = null
    if (wheelTimer.current != null) window.clearTimeout(wheelTimer.current)
    wheelTimer.current = null; setAxisInteracting(false); setPricePreview(null); setTimePreview(null)
  }, [replayWindow.window_id, symbol])
  useLayoutEffect(() => () => { if (wheelTimer.current != null) window.clearTimeout(wheelTimer.current) }, [])
  useLayoutEffect(() => {
    if (!holder.current) return
    const element = holder.current
    const measure = () => setSize(previous => {
      const width = Math.max(240, element.clientWidth), height = Math.max(90, element.clientHeight)
      return width === previous.width && height === previous.height ? previous : { width, height, revision: previous.revision + 1 }
    })
    const repaint = () => setSize(previous => ({ ...previous, revision: previous.revision + 1 }))
    measure()
    const resize = new ResizeObserver(measure); resize.observe(element)
    const theme = new MutationObserver(repaint); theme.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style'] })
    document.fonts.addEventListener('loadingdone', repaint)
    return () => { resize.disconnect(); theme.disconnect(); document.fonts.removeEventListener('loadingdone', repaint) }
  }, [])

  useLayoutEffect(() => {
    if (!holder.current || !heatView || !heat) { historyLayers.current = []; return }
    const theme = readCanvasTheme(holder.current), css = getComputedStyle(holder.current)
    historyLayers.current = [false, true].map(future => {
      const layer = document.createElement('canvas'), ctx = prepareCanvas(layer, size.width, height)
      if (!ctx) return layer
      const up = future ? theme.ink : css.getPropertyValue('--color-up').trim()
      const down = future ? theme.ink : css.getPropertyValue('--color-down').trim()
      ctx.save(); ctx.beginPath(); ctx.rect(layout.left, layout.top, layout.right - layout.left, layout.bottom - layout.top); ctx.clip()
      const maxQuantity = Math.max(1, maxHeat)
      ctx.save(); ctx.beginPath(); ctx.rect(layout.left, layout.top, Math.max(0, replayTimeX(drawThrough, heatRange, layout) - layout.left), layout.bottom - layout.top); ctx.clip()
      ctx.fillStyle = future ? theme.ink : theme.accent
      for (let i = 0; i < heat.data.length; i += 6) {
        const x = heat.data[i], y = heat.data[i + 1], quantity = heat.data[i + 2], unknown = heat.data[i + 3]
        ctx.globalAlpha = Number.isNaN(quantity) ? 0.12 : 0.025 + quantity / maxQuantity * 0.825
        ctx.fillRect(x, y - heat.rowHeight / 2, 3, heat.rowHeight)
        if (unknown) { ctx.globalAlpha = 0.5; ctx.fillRect(x, y - heat.rowHeight / 2, 1, heat.rowHeight) }
      }
      ctx.restore()
      for (const gap of replayWindow.gaps) {
        if ((gap.symbol && gap.symbol !== symbol) || gap.start_us > drawThrough || (gap.topic && gap.topic !== 'depth' && !gap.topic.startsWith('/quote'))) continue
        const x = replayTimeX(Math.max(heatRange.start_us, gap.start_us), heatRange, layout), end = replayTimeX(Math.min(drawThrough, gap.end_us), heatRange, layout)
        ctx.fillStyle = theme.fill; ctx.fillRect(x, layout.top, Math.max(1, end - x), layout.bottom - layout.top)
      }
      for (const side of ['买', '卖'] as const) {
        ctx.strokeStyle = side === '买' ? up : down; ctx.lineWidth = 1.3
        historyQuotes.forEach((quote, index) => {
          const best = replayBestLevel(quote, side), next = historyQuotes[index + 1]
          if (!best || !currentReplayQuote(quote, quote.time_us, replayWindow.gaps)) return
          const end = replayQuoteEnd(quote, next?.time_us ?? drawThrough, drawThrough, replayWindow.gaps)
          if (end < heatRange.start_us || quote.time_us >= heatRange.end_us || (!quote.book_valid || !quote.available)) return
          const x = replayTimeX(Math.max(heatRange.start_us, quote.time_us), heatRange, layout), right = replayTimeX(Math.min(heatRange.end_us, end), heatRange, layout)
          const y = replayPriceY(best.price, heatView, layout)
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(Math.max(x + 1, right), y)
          const nextBest = next && currentReplayQuote(next, next.time_us, replayWindow.gaps) && replayBestLevel(next, side)
          if (nextBest && end === next.time_us && quote.recording_id === next?.recording_id) ctx.lineTo(right, replayPriceY(nextBest.price, heatView, layout))
          ctx.stroke()
        })
      }
      ctx.strokeStyle = future ? theme.ink : css.getPropertyValue('--color-ink-1').trim(); ctx.lineWidth = 1.5
      for (const segment of marketSegments) {
        ctx.beginPath()
        segment.forEach((trade, index) => {
          const x = replayTimeX(trade.time_us, heatRange, layout), y = replayPriceY(trade.price!, heatView, layout)
          if (index === 0) ctx.moveTo(x, y)
          else ctx.lineTo(x, y)
        })
        ctx.stroke()
      }
      ctx.restore()
      return layer
    })
  }, [size, height, heatView, heat, maxHeat, layout, drawThrough, heatRange, replayWindow.gaps, symbol, historyQuotes, marketSegments])

  useLayoutEffect(() => {
    if (!holder.current || !historyView) { orderLayers.current = []; return }
    const theme = readCanvasTheme(holder.current), css = getComputedStyle(holder.current)
    orderLayers.current = [false, true].map(future => {
      const layer = document.createElement('canvas'), ctx = prepareCanvas(layer, size.width, height)
      if (ctx) {
        for (const bubble of bubbles) {
          ctx.fillStyle = ctx.strokeStyle = future || replayFutureRecord(bubble.time_us, bubble.trades[0].capture_seq, bubbleCursor!) ? theme.ink : bubble.direction === 'BUY' ? css.getPropertyValue('--color-up').trim() : bubble.direction === 'SELL' ? css.getPropertyValue('--color-down').trim() : theme.ink
          ctx.save(); ctx.shadowColor = theme.bg; ctx.shadowBlur = 3; ctx.shadowOffsetY = 2; ctx.globalAlpha = .85; ctx.beginPath()
          ctx.arc(replayTimeX(bubble.time_us, historyRange, layout), replayPriceY(bubble.price, historyView, layout), replayBubbleRadius(bubble.quantity, maxBubble), 0, Math.PI * 2)
          if (bubble.stable) ctx.fill()
          ctx.shadowBlur = 0; ctx.globalAlpha = 1
          if (bubble.stable) ctx.strokeStyle = theme.surface
          ctx.lineWidth = 1.5; ctx.stroke(); ctx.restore()
        }
        ctx.globalAlpha = 1
        ctx.font = `12px ${theme.font}`
        drawOrderHistory(ctx, visibleTimeline, historyRange, historyView, layout, selectedOrder, executionId,
          future ? { ...theme, warn: theme.ink } : theme, future ? theme.ink : css.getPropertyValue('--color-chart-2').trim(), { time_us: Infinity })
      }
      return layer
    })
  }, [size, height, historyView, visibleTimeline, historyRange, layout, selectedOrder, executionId, bubbles, maxBubble, bubbleCursor])

  useLayoutEffect(() => {
    if (!canvas.current || !holder.current) return
    const ctx = prepareCanvas(canvas.current, size.width, height)
    if (!ctx) return
    const theme = readCanvasTheme(holder.current), css = getComputedStyle(holder.current)
    ctx.font = `12px ${theme.font}`; ctx.fillStyle = theme.surface; ctx.fillRect(0, 0, size.width, height)
    ctx.fillStyle = theme.ink
    if (!axisInteracting) {
      // 数字使用等宽槽位，手势结束后重新适配，不累积历史最大宽度。
      ctx.font = `500 12px ${theme.font}`
      const digitWidth = Math.max(...Array.from('0123456789', digit => ctx.measureText(digit).width))
      const axisPrices = [...priceTicks, ...orderPrices.filter(price => view && price >= view.min && price <= view.max), ...(view ? [view.min, view.max, ...[view.min, view.max].map(price => Math.round(price / priceTick) * priceTick)] : [])]
      const textWidths = axisPrices.map(price => Array.from(axisPriceText(price)).reduce((width, char) => width + (/[0-9]/.test(char) ? digitWidth : ctx.measureText(char).width), 0))
      const measuredAxisWidth = Math.max(40, Math.ceil(Math.max(0, ...textWidths)) + 12)
      if (measuredAxisWidth !== priceAxisWidth) { setPriceAxisWidth(measuredAxisWidth); return }
    }
    ctx.font = `11px ${css.getPropertyValue('--font-mono')}`
    const measuredCharacterWidth = ctx.measureText('00:00:00.000000').width / 15
    if (measuredCharacterWidth !== timeCharacterWidth) setTimeCharacterWidth(measuredCharacterWidth)
    ctx.font = `12px ${theme.font}`
    if (!view || !heat) { ctx.fillText(!view ? '这段记录尚无有效价位。' : heatError ?? '历史深度计算中', layout.left, height / 2); return }
    const splitX = Math.max(layout.left, Math.min(layout.right, cursorX))
    for (const future of [false, true]) {
      const paint = future ? { ...theme, accent: theme.ink, warn: theme.ink } : theme
      ctx.save(); ctx.beginPath(); ctx.rect(future ? splitX : layout.left, layout.top, future ? layout.right - splitX : splitX - layout.left, layout.bottom - layout.top); ctx.clip()
      const history = historyLayers.current[Number(future)]
      if (history && historyView) {
        // shortcut: 手势中只变换已有历史，新增视野在手势结束后精确补绘。
        const scale = (heatView!.max - heatView!.min) / (view.max - view.min)
        const timeScale = (heatRange.end_us - heatRange.start_us) / (visibleRange.end_us - visibleRange.start_us)
        const left = replayTimeX(heatRange.start_us, visibleRange, layout) - layout.left * timeScale
        const top = replayPriceY(heatView!.max, view, layout)
        ctx.drawImage(history, left, top - layout.top * scale, size.width * timeScale, height * scale)
      }
      const orders = orderLayers.current[Number(future)]
      if (orders && historyView) {
        const scale = (historyView.max - historyView.min) / (view.max - view.min)
        const timeScale = (historyRange.end_us - historyRange.start_us) / (visibleRange.end_us - visibleRange.start_us)
        ctx.drawImage(orders, replayTimeX(historyRange.start_us, visibleRange, layout) - layout.left * timeScale,
          replayPriceY(historyView.max, view, layout) - layout.top * scale, size.width * timeScale, height * scale)
      }
      // 时间裁剪无法区分同刻采集序号，只补画尚未到达的委托及命中标记。
      const sameTime = projection.capture_seq == null ? undefined : orderHighlights.byTime.get(projection.time_us)
      if (!future && sameTime) drawOrderHistory(ctx, { segments: sameTime.segments.filter(segment => segment.order.capture_seq > projection.capture_seq!), marks: sameTime.marks.filter(mark => mark.order.capture_seq > projection.capture_seq!) }, visibleRange, view, layout, selectedOrder, executionId, paint, theme.ink, projection)
      ctx.restore()
    }
    if (selectedExecution) {
      const future = replayFutureRecord(selectedExecution.time_us, selectedExecution.capture_seq, projection)
      ctx.fillStyle = future ? theme.ink : theme.accent
      ctx.fillText(`${future ? '未到达 · ' : ''}选中逐笔 ${replayTime(selectedExecution.time_us)} · ${quantityText(selectedExecution.quantity)} @ ${priceText(selectedExecution.price)}`, layout.left, 18, layout.right - layout.left)
    }
    for (const price of priceTicks) {
      const y = replayPriceY(price, view, layout)
      canvasLine(ctx, layout.left, y, layout.right + 3, y, theme.line)
    }
    canvasLine(ctx, layout.right, layout.top, layout.right, layout.bottom, theme.line)
  }, [axisPriceText, axisInteracting, instrument.price_decimals, instrument.price_tick, priceTick, priceAxisWidth, timeCharacterWidth, priceTicks, orderPrices, size, height, projection, replayWindow, visibleRange, symbol, executionId, selectedOrder, selectedExecution, view, layout, historyQuotes, timeline, orderHighlights, marketSegments, priceText, quantityText, heat, heatError, maxHeat, heatView, heatRange, historyView, historyRange, recorded, drawThrough, cursorX, bubbles, maxBubble])

  useLayoutEffect(() => {
    if (!hoverCanvas.current || !holder.current) return
    const ctx = prepareCanvas(hoverCanvas.current, size.width, height)
    if (!ctx || !view || !hoveredObject) return
    const theme = readCanvasTheme(holder.current), css = getComputedStyle(holder.current)
    const splitX = Math.max(layout.left, Math.min(layout.right, cursorX))
    ctx.font = `12px ${theme.font}`
    for (const future of [false, true]) {
      ctx.save(); ctx.beginPath(); ctx.rect(future ? splitX : layout.left, layout.top, future ? layout.right - splitX : splitX - layout.left, layout.bottom - layout.top); ctx.clip()
      for (const bubble of bubbles) {
        if (hoveredObject !== `bubble:${bubble.trades[0].recording_id ? `${bubble.trades[0].recording_id}:` : ''}${bubble.trades[0].capture_seq}:${bubble.price}:${bubble.direction}`) continue
        ctx.strokeStyle = future || replayFutureRecord(bubble.time_us, bubble.trades[0].capture_seq, projection) ? theme.ink : theme.accent
        ctx.lineWidth = 2; ctx.beginPath()
        ctx.arc(replayTimeX(bubble.time_us, visibleRange, layout), replayPriceY(bubble.price, view, layout), replayBubbleRadius(bubble.quantity, maxBubble), 0, Math.PI * 2); ctx.stroke()
      }
      const hovered = orderHighlights.byObject.get(hoveredObject)
      if (hovered) drawOrderHistory(ctx, hovered, visibleRange, view, layout, selectedOrder, executionId,
        future ? { ...theme, accent: theme.ink, warn: theme.ink } : theme, future ? theme.ink : css.getPropertyValue('--color-chart-2').trim(), projection, hoveredObject)
      ctx.restore()
    }
  }, [size, height, view, hoveredObject, layout, cursorX, bubbles, maxBubble, projection, visibleRange, orderHighlights, selectedOrder, executionId])

  const resetView = () => { setManualView(null); setTimeView(null); setHover(null) }
  const zoom = (factor: number) => {
    if (view) setManualView(zoomReplayPrice(view, factor))
    setTimeView(zoomReplayTime(visibleRange, factor, (visibleRange.start_us + visibleRange.end_us) / 2, range))
  }
  const pan = (dx: number, dy: number) => {
    if (view) { const offset = (view.max - view.min) * dy; setManualView({ ...view, min: view.min + offset, max: view.max + offset }) }
    const offset = (visibleRange.end_us - visibleRange.start_us) * dx
    setTimeView(clampReplayView({ start_us: visibleRange.start_us + offset, end_us: visibleRange.end_us + offset }, range))
  }
  const inspectAt = (x: number, y: number, select = false) => {
    if (heatPending) { setHover(null); if (select) setInspection(null); return }
    if (!view || y < layout.top || y > layout.bottom || x < layout.left || x > layout.right && x <= layout.priceX + 8) { setHover(null); if (select) setInspection(null); return }
    const time = x > layout.right ? projection.time_us : replayXTime(x, visibleRange, layout)
    if (time > drawThrough) { setHover(null); if (select) setInspection(null); return }
    const price = replayYPrice(y, view, layout)
    const format = { executionId, cursor: projection, unit: instrument.quantity_label ?? '', price: priceText, quantity: quantityText }
    let records: InspectionRecord[] = [], candidates: InspectionRecord[] = []
    if (x <= layout.right) {
      candidates = visibleTimeline.marks.filter(mark => Math.hypot(replayTimeX(mark.time, visibleRange, layout) - x, replayPriceY(mark.price, view, layout) - y) <= 18)
        .map(mark => inspectReplayOrder(mark.order, format, replayTimeX(mark.time, visibleRange, layout), replayPriceY(mark.price, view, layout)))
      if (!candidates.length) candidates = timeline.segments.filter(segment => time >= segment.start && time <= segment.end && segment.order.price != null && Math.abs(replayPriceY(segment.order.price, view, layout) - y) <= 6)
        .map(segment => inspectReplayOrder(segment.order, format, x, replayPriceY(segment.order.price!, view, layout)))
      const hitKeys = new Set(candidates.map(record => record.order))
      records = historyOrders.filter(order => hitKeys.has(order.key)).map(order => inspectReplayOrder(order, format, replayTimeX(order.time_us, visibleRange, layout), replayPriceY(order.price ?? price, view, layout)))
      if (layers.execution && replayWindow.context.streams?.[symbol]?.own_trade?.availability !== 'unavailable') {
        for (const trade of recorded.ownTrades.filter(trade => trade.symbol === symbol)) {
          const owner = historyOrders.find(order => order.order_id === trade.order_id && hitKeys.has(order.key))
          if (owner) records.push(inspectReplayTrade(trade, false, `order:${owner.key}`, format, replayTimeX(trade.time_us, visibleRange, layout), replayPriceY(trade.price ?? price, view, layout)))
        }
      }
      for (const bubble of bubbles.filter(item => Math.hypot(replayTimeX(item.time_us, visibleRange, layout) - x, replayPriceY(item.price, view, layout) - y) <= Math.max(18, replayBubbleRadius(item.quantity, maxBubble)))) {
        const object = `bubble:${bubble.trades[0].recording_id ? `${bubble.trades[0].recording_id}:` : ''}${bubble.trades[0].capture_seq}:${bubble.price}:${bubble.direction}`
        const bx = replayTimeX(bubble.time_us, visibleRange, layout), by = replayPriceY(bubble.price, view, layout)
        const direction = bubble.direction === 'BUY' ? '主动买' : bubble.direction === 'SELL' ? '主动卖' : '方向未知'
        const summary: InspectionRecord = { id: object, object, kind: 'market', title: `市场成交 · ${direction}`,
          headline: `${quantityText(bubble.quantity)} ${format.unit} @ ${priceText(bubble.price)}`, status: `${bubble.trades.length} 条市场记录 · ${direction}`,
          label: `${bubble.trades.length} 条市场记录 · ${direction}`, raw: `${replayTime(bubble.time_us)}–${replayTime(bubble.end_us)}；按时间像素、真实价格和方向聚合，源聚合记录不拆逐笔。`,
          time: bubble.time_us, seq: bubble.trades[0].capture_seq, future: replayFutureRecord(bubble.time_us, bubble.trades[0].capture_seq, projection), x: bx, y: by, radius: replayBubbleRadius(bubble.quantity, maxBubble) }
        candidates.push(summary); records.push(summary, ...bubble.trades.map(trade => inspectReplayTrade(trade, true, object, format, replayTimeX(trade.time_us, visibleRange, layout), replayPriceY(trade.price!, view, layout))))
      }
    } else if (layout.columns) {
      records = ownOrders.filter(order => order.price != null && Math.abs(replayPriceY(order.price, view, layout) - y) <= 10).map(order => inspectReplayOrder(order, format, x, replayPriceY(order.price!, view, layout)))
      candidates = records
    }
    const quote = x > layout.right ? currentQuote : currentReplayQuote(historyQuotes[replayTimeUpperBound(historyQuotes, time) - 1], time, replayWindow.gaps)
    const level = quote && recordedDepth(quote).find(item => Math.abs(replayPriceY(item.price, view, layout) - y) <= Math.max(3, (layout.bottom - layout.top) * view.tick / (view.max - view.min) / 2))
    const fallback: InspectionRecord = { id: level ? 'depth' : 'pointer', object: 'depth', kind: 'depth', title: level ? `${x > layout.right ? '游标盘口' : '历史盘口'} · ${level.side}${level.rank ?? '·0'}` : '指针价格',
      headline: priceText(level?.price ?? Math.round(price / (instrument.price_tick ?? 10 ** -decimals)) * (instrument.price_tick ?? 10 ** -decimals)), status: level ? `挂量 ${quantityText(level.quantity)} ${format.unit}` : '挂量未知／未覆盖', label: '盘口观察', raw: '仅表示该时刻已录到的档位；档位不代表排队顺序。',
      time, future: replayFutureRecord(time, 0, projection), x, y: level ? replayPriceY(level.price, view, layout) : y }
    const hit = nearestReplayInspection(candidates, x, y, activeHover?.id) ?? fallback
    const next = { cursor: hoverCursor, ...hit, records: [hit, ...records.filter(record => record.id !== hit.id)] }
    setHover(next)
    if (select) {
      if (next.kind === 'depth') setInspection(null)
      else { onInspect(); setInspection({ record: next, records: next.records, x, y }) }
    }
    return next
  }
  const pointerPosition = (event: PointerEvent<HTMLCanvasElement>) => { const bounds = event.currentTarget.getBoundingClientRect(); return { x: event.clientX - bounds.left, y: event.clientY - bounds.top } }
  const endGesture = () => { viewScheduler.flush(); seekScheduler.flush(); gesture.current = null; if (wheelTimer.current == null) { setAxisInteracting(false); setPricePreview(null); setTimePreview(null) } }
  const pointerMove = (event: PointerEvent<HTMLCanvasElement>) => {
    const { x, y } = pointerPosition(event), drag = gesture.current
    event.currentTarget.style.cursor = view && y >= layout.top && x > layout.right && x <= layout.priceX + 8 ? 'ns-resize' : 'crosshair'
    if (!drag) { if (!heatPending) inspectAt(x, y); return }
    const dx = x - drag.x, dy = y - drag.y
    if (!drag.moved && Math.hypot(dx, dy) < 3) return
    drag.moved = true; setHover(null); setInspection(null)
    if (drag.mode === 'seek') { seekScheduler.schedule(replayXTime(x, drag.range, layout)); return }
    if (drag.mode === 'price') viewScheduler.schedule({ view: zoomReplayPriceAt(drag.view, Math.exp(dy / 160), replayYPrice(drag.y, drag.view, layout)) })
    else if (drag.mode === 'time') viewScheduler.schedule({ range: zoomReplayTime(drag.range, Math.exp(dx / 200), replayXTime(drag.x, drag.range, layout), range) })
    else {
      const shift = -dx / (layout.right - layout.left) * (drag.range.end_us - drag.range.start_us)
      const priceShift = dy / (layout.bottom - layout.top) * (drag.view.max - drag.view.min)
      viewScheduler.schedule({ range: clampReplayView({ start_us: drag.range.start_us + shift, end_us: drag.range.end_us + shift }, range), view: { ...drag.view, min: drag.view.min + priceShift, max: drag.view.max + priceShift } })
    }
  }
  useLayoutEffect(() => {
    const element = canvas.current
    if (!element) return
    const wheel = (event: WheelEvent) => {
      if (!view) return
      const bounds = element.getBoundingClientRect(), x = event.clientX - bounds.left, y = event.clientY - bounds.top
      if (x < layout.left || x > layout.priceX + 8 || y < layout.top) return
      event.preventDefault(); setHover(null); setInspection(null); setAxisInteracting(true)
      setPricePreview(previous => previous ?? view); setTimePreview(previous => previous ?? visibleRange)
      if (wheelTimer.current != null) window.clearTimeout(wheelTimer.current)
      wheelTimer.current = window.setTimeout(() => { wheelTimer.current = null; if (!gesture.current) { setAxisInteracting(false); setPricePreview(null); setTimePreview(null) } }, 120)
      const delta = (event.deltaY || event.deltaX) * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? size.height : 1)
      const factor = Math.exp(Math.max(-1, Math.min(1, delta / 400)))
      if (x >= layout.left && x <= layout.right) setTimeView(zoomReplayTime(visibleRange, factor, replayXTime(x, visibleRange, layout), range))
      if (y <= layout.bottom) setManualView(zoomReplayPriceAt(view, factor, replayYPrice(y, view, layout)))
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [view, visibleRange, range, layout, size.height])
  const outside = view ? ownOrders.filter(order => order.price != null && (order.price < view.min || order.price > view.max)) : []
  const bookRows: { price: number; quantity: number | null; side: string }[] = displayQuote ? [...displayQuote.asks.map(level => ({ ...level, side: '卖' })), ...displayQuote.bids.map(level => ({ ...level, side: '买' }))] : []
  for (const order of depthOrders) if (order.price != null && !bookRows.some(level => level.price === order.price)) bookRows.push({ price: order.price, quantity: null, side: '档外' })
  bookRows.sort((a, b) => b.price - a.price)
  const outsideDepth = displayQuote && view ? recordedDepth(displayQuote).filter(level => level.price < view.min || level.price > view.max).length : 0
  const focusOrder = () => {
    if (!selected || !view) return
    onInspect(); setTimeView(replayOrderFocus(selected, scene, range)); setHover(null)
    if (selected.price != null && (selected.price < view.min || selected.price > view.max)) setManualView({ ...view, min: Math.min(view.min, selected.price - view.tick * 2), max: Math.max(view.max, selected.price + view.tick * 2) })
  }

  return <section aria-label="历史深度与自有委托" className="replay-chart" >
    <div className="replay-chart-playback">{symbolControl}{children}</div>
    <div className="replay-chart-meta"><div className="replay-chart-meta-main"><div className="replay-chart-legend"><span className="replay-depth-bid"><Minus size={14} aria-hidden />买一</span><span className="replay-depth-ask"><Minus size={14} aria-hidden />卖一</span><span><Minus size={14} aria-hidden />成交价</span>{layers.market && <span><Circle size={10} aria-hidden />市场成交</span>}<span className="replay-order-color"><Minus size={14} aria-hidden />委托</span><span><Minus size={14} aria-hidden />未到达</span></div><span className="replay-hover-time">指向 <time className="num" aria-label={activeHover ? `悬停时间 ${replayTime(activeHover.time)}` : '未指向图表'}>{activeHover ? replayTime(activeHover.time) : '—'}</time></span>      <div className="replay-chart-view-controls replay-chart-navigation" role="group" aria-label="图表导航">
      <div className="replay-chart-return-slot">      {!cursorInside && <div className="replay-chart-return">
        <Tooltip content={`游标在${projection.time_us < visibleRange.start_us ? '左' : '右'}侧 · 回到游标`}><button type="button" className={controlClass} aria-label="回到游标" onClick={() => { const span = visibleRange.end_us - visibleRange.start_us; setTimeView(clampReplayView({ start_us: projection.time_us - span / 2, end_us: projection.time_us + span / 2 }, range)) }}>{projection.time_us < visibleRange.start_us ? <ArrowLeft size={15} aria-hidden /> : <ArrowRight size={15} aria-hidden />}</button></Tooltip>
      </div>}
      </div>
        <div className="replay-playback-group" role="group" aria-label="回放与视图控制">
        {playControls}
        <Tooltip content="恢复视图"><button type="button" className={controlClass} onClick={resetView} disabled={!manualView && !timeView} aria-label="恢复视图"><RotateCcw size={15} aria-hidden /></button></Tooltip>
        <Tooltip content="聚焦委托过程"><button type="button" className={controlClass} aria-label="聚焦委托过程" disabled={!view || !selected} onClick={focusOrder}><Crosshair size={14} aria-hidden /></button></Tooltip>
        <Tooltip content="图层与图例说明"><button type="button" className={controlClass} popoverTarget="replay-chart-legend" aria-label="图层与图例说明"><Info size={15} aria-hidden /></button></Tooltip>
        </div>
        {firstQuoteControl}
        {fullscreenControl}
      </div>
    </div><div className="replay-depth-status">{outsideDepth > 0 && <span className="replay-depth-fit-slot"><Tooltip content={`当前视野外有 ${outsideDepth} 个已录档位`}><button type="button" className="text-accent focus-visible:outline-2 focus-visible:outline-accent" style={{ visibility: outsideDepth > 0 ? 'visible' : 'hidden' }} disabled={outsideDepth === 0} onClick={() => setManualView(null)}>显示全部档位</button></Tooltip></span>}<span className="replay-heat-scale"><span className="replay-heat-caption"><span>{heatError ?? (heatPending || axisInteracting ? heat ? '历史缩放预览 · 结束后补绘' : '历史深度计算中' : heat?.known ? '挂量峰值' : '挂量尚未知')}</span></span><span className="replay-heat-legend" style={{ visibility: heat?.known ? 'visible' : 'hidden' }} aria-hidden={!heat?.known}><i aria-hidden /><span><span>0</span><span>{quantityText(maxHeat / 2)}</span><span>{quantityText(maxHeat)}</span></span></span></span></div></div>
    <div ref={holder} className={`replay-chart-plot${resizingDepth ? ' select-none' : ''}`}>
      <p id="replay-scene-summary" className="sr-only">已读取时段，游标之后的记录灰显为未到达，已录自有成交 {recorded.ownTrades.filter(trade => trade.symbol === symbol && trade.time_us >= visibleRange.start_us && trade.time_us < visibleRange.end_us).length} 笔。盘口与委托状态对应游标 {replayTime(projection.time_us)}。</p>
      <canvas ref={canvas} style={{ height }} className="block w-full touch-none cursor-crosshair focus-visible:outline-2 focus-visible:outline-accent" tabIndex={0}
        aria-describedby="replay-scene-summary"
        aria-label="历史盘口热力图与共享价格轴；拖动浏览，滚轮缩放，方向键平移，正负键缩放，Home 复位" role="group"
        onPointerDown={event => { if (!view || event.button !== 0) return; const { x, y } = pointerPosition(event); if (y < layout.top) return; if (x < layout.left || x > layout.priceX + 8) return; event.currentTarget.focus(); event.currentTarget.setPointerCapture(event.pointerId); setAxisInteracting(true); setPricePreview(view); setTimePreview(visibleRange); gesture.current = { id: event.pointerId, x, y, view, range: visibleRange, mode: x > layout.right ? 'price' : cursorInside && Math.abs(x - replayTimeX(projection.time_us, visibleRange, layout)) <= 8 ? 'seek' : y > layout.bottom ? 'time' : 'chart', moved: false } }}
        onPointerMove={pointerMove} onPointerLeave={() => { if (!gesture.current) setHover(null) }}
        onPointerUp={event => { const drag = gesture.current; if (drag?.mode === 'seek' && drag.moved) seekScheduler.schedule(replayXTime(pointerPosition(event).x, drag.range, layout)); endGesture(); if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); if (drag && !drag.moved) { const { x, y } = pointerPosition(event); if (x >= layout.left && x <= layout.right) inspectAt(x, y, true) } }}
        onPointerCancel={() => { endGesture(); setHover(null) }} onLostPointerCapture={endGesture}
        onKeyDown={event => {
          if (event.key.startsWith('Arrow')) { event.preventDefault(); pan(event.key === 'ArrowLeft' ? -0.15 : event.key === 'ArrowRight' ? 0.15 : 0, event.key === 'ArrowUp' ? 0.15 : event.key === 'ArrowDown' ? -0.15 : 0) }
          else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(0.7) }
          else if (event.key === '-') { event.preventDefault(); zoom(1.4) }
          else if (event.key === 'Home') { event.preventDefault(); resetView() }
          else if (event.key === 'Enter' && view) { event.preventDefault(); inspectAt(replayTimeX(projection.time_us, visibleRange, layout), replayPriceY(selected?.price ?? (view.min + view.max) / 2, view, layout), true) }
          else if (event.key === 'Escape') { setHover(null); setInspection(null) }
        }} />
      <canvas ref={hoverCanvas} aria-hidden="true" className="pointer-events-none absolute left-0 top-0 w-full" style={{ height }} />
      <div className="replay-price-axis" aria-hidden="true" style={{ left: layout.right, top: layout.top, height: layout.bottom - layout.top }} />
      {view && priceTicks.map(price => {
        if (orderPrices.includes(price)) return null
        const top = priceLabelTop(replayPriceY(price, view, layout))
        return <span key={price} className="replay-price-tick num" aria-hidden="true" style={{ ...priceLabelStyle, top, opacity: priceLabelOpacity(top, occupiedPrices) }}>{axisPriceText(price)}</span>
      })}
      {orderLabels.map(label => <span key={label.price} className="replay-order-price num" data-pending={label.pending || undefined} data-collapsed={label.collapsed || undefined}
        style={{ ...priceLabelStyle, top: label.top }} aria-label={`账户挂单价格 ${priceText(label.price)}`}>
        <i aria-hidden="true" style={{ top: label.y - label.top }} />
        <span style={{ opacity: label.collapsed ? 0 : priceLabelOpacity(label.top, hoverOccupied) }}>{axisPriceText(label.price)}</span>
        <b className="replay-axis-name" style={{ opacity: label.collapsed ? 0 : priceLabelOpacity(label.top, hoverOccupied) }}>委托</b>
      </span>)}
      <div className="replay-time-ruler" aria-hidden="true" style={{ left: layout.left, width: layout.right - layout.left, top: axisY }}>
        {timeTicks.map(tick => <span key={tick.time} className="replay-time-tick" style={{ left: tick.x - layout.left }}><i /><time className="num" style={{ left: tick.left - tick.x, width: tick.width, opacity: replayTimeTickOpacity(tick.left, tick.width, cursorLeft, cursorWidth) }}>{tick.label}</time></span>)}
      </div>
      {cursorInside && <div className="replay-cursor-line" aria-hidden="true" style={{ left: cursorX, top: layout.top, height: axisY - layout.top }} />}
      <time className="replay-cursor-time num" data-outside={!cursorInside || undefined} aria-label={`当前回放时间 ${replayTime(projection.time_us)}${cursorInside ? '' : '，游标在当前视野之外'}`} style={{ left: cursorLeft, width: cursorWidth, top: axisY + 1 }}>{!cursorInside && projection.time_us < visibleRange.start_us ? '← ' : ''}{replayTime(projection.time_us)}{!cursorInside && projection.time_us >= visibleRange.end_us ? ' →' : ''}</time>
      <div className="replay-chart-axis" data-outside={!cursorInside || undefined}
        onPointerDownCapture={event => { if (event.isPrimary && event.button === 0) sliderDragging.current = true }}
        onPointerUp={() => { seekScheduler.flush(); sliderDragging.current = false }}
        onPointerCancel={() => { seekScheduler.flush(); sliderDragging.current = false }}
        onLostPointerCapture={() => { seekScheduler.flush(); sliderDragging.current = false }} style={{ left: layout.left - 12, width: (layout.right - layout.left) * Math.max(0, visibleRange.end_us - visibleRange.start_us - 1) / (visibleRange.end_us - visibleRange.start_us) + 24, top: axisY - 22 }}>
        <ValueSlider variant="axis" label="录制定位" min={0} max={visibleRange.end_us - visibleRange.start_us - 1} step={1} value={projection.time_us - visibleRange.start_us} disabled={visibleRange.end_us - visibleRange.start_us <= 1}
          formatValue={offset => cursorInside ? replayTime(visibleRange.start_us + offset) : `游标在当前视野之外，${replayTime(projection.time_us)}`} onChange={offset => { setHover(null); setInspection(null); const time = Math.min(visibleRange.end_us - 1, visibleRange.start_us + offset); if (sliderDragging.current) seekScheduler.schedule(time); else onSeek(time) }}
          onChangeEnd={() => { seekScheduler.flush(); sliderDragging.current = false }} />
      </div>
      {layout.columns && <ResizeHandle orientation="vertical" aria-label="调整图表与盘口的宽度"
        title="左右拖动调整盘口宽度 · 双击恢复默认" className="replay-depth-resize"
        style={{ left: layout.priceX + 13, height }} value={clampDepthWidth(depthWidth)}
        min={346} max={maxDepthWidth} step={-24} onChange={setDepthWidth} onCommit={saveDepthWidth}
        onReset={() => saveDepthWidth(346)} onResizeChange={setResizingDepth}
        valueAt={x => clampDepthWidth(size.width - (x - (holder.current?.getBoundingClientRect().left ?? 0)) + 8)} />}
      {view && layout.columns && <ReplayDepthLadder quote={displayQuote} future={futureDepth} orders={depthOrders} view={view} layout={layout} height={height} executionId={executionId} selected={selectedOrder} onSelect={key => { onInspect(); onSelectOrder(key) }} priceText={priceText} quantityText={quantityText} hoverPrice={hoverPrice ?? undefined} />}
      {inspection && <div className="replay-inspection-float" aria-label="图中记录浮卡" style={{
        width: inspectionWidth,
        left: Math.max(8, Math.min(layout.right - inspectionWidth - 8, inspection.x + 12 + inspectionWidth <= layout.right ? inspection.x + 12 : inspection.x - 12 - inspectionWidth)),
        top: Math.max(8, Math.min(height - 400, inspection.y + 12)),
        maxHeight: Math.max(0, height - Math.max(8, Math.min(height - 400, inspection.y + 12)) - 8),
      }}>
        <ReplayInspector record={inspection.record} records={inspection.records} onChoose={record => setInspection({ ...inspection, record })}
          onClose={() => { setInspection(null); canvas.current?.focus() }}
          onView={() => { if (inspection.record.trade) onSelectTrade(inspection.record.trade); else if (inspection.record.order) onSelectOrder(inspection.record.order); setInspection(null) }}
          onSeek={() => { onSeek(inspection.record.time, inspection.record.seq); setInspection(null) }} />
      </div>}
      {activeHover && <>
        {activeHover.x <= layout.right && <div className="replay-crosshair" aria-hidden style={{ left: activeHover.x, top: layout.top, height: axisY - layout.top }} />}
        <div className="replay-crosshair-horizontal" aria-hidden style={{ left: layout.left, right: size.width - layout.right, top: activeHover.y }} />
        <span className={`replay-crosshair-price num${hoverOrder ? ' replay-order-price' : ''}`} data-pending={hoverOrder?.pending || undefined} aria-label={`悬停价格 ${axisPriceText(hoverPrice)}`} style={{ ...priceLabelStyle, top: hoverPriceTop ?? undefined }}><i aria-hidden="true" style={{ top: activeHover.y - (hoverPriceTop ?? 0) }} /><span>{view && axisPriceText(hoverPrice)}</span>{hoverOrder && <b className="replay-axis-name">委托</b>}</span>
      </>}
    </div>
    <div className="replay-outside-orders flex flex-wrap gap-2" aria-label="价位范围之外的自有委托">{outside.map(order => <button key={order.key} className="min-h-8 cursor-pointer rounded-chip bg-fill px-2 text-xs text-ink-2 focus-visible:outline-2 focus-visible:outline-accent" onClick={() => { onSelectOrder(order.key); if (view && order.price != null) setManualView(centerReplayPrice(view, order.price)) }}>{order.price! > view!.max ? '上方' : '下方'} {order.order_id} · {replayOwnership(order, executionId).label} · {priceText(order.price)} · 定位</button>)}</div>
    <div className={'sr-only'}><table className="num w-full text-right text-xs"><caption className="mb-2 text-left text-ink-2">{futureDepth ? '未到达 · 首个已录盘口' : '当前游标'} · 价格 / COB / ORD{instrument?.quantity_label ? `（${instrument.quantity_label}）` : ''}</caption><thead><tr className="text-ink-2"><th className="py-2 text-left">方向</th><th>价格</th><th>市场量</th><th>账户挂单</th></tr></thead><tbody>{bookRows.map((level, index) => {
      const own = depthOrders.filter(order => order.price === level.price)
      return <tr key={`${level.side}:${level.price}:${index}`} className="border-t border-line"><th className="py-2 text-left font-normal">{level.side}</th><td>{priceText(level.price)}</td><td>{quantityText(level.quantity)}</td><td>{own.length ? replayOrderOwnershipGroups(own, executionId).map(group => <span key={group.kind} className="block">{group.label} {quantityText(replayRemainingQuantity(group.orders))} · {group.orders.length}单</span>) : '—'}</td></tr>
    })}</tbody></table>{!displayQuote && <p className="py-3 text-xs text-ink-2">盘口未记录／已失效；自有委托仍可在订单表查看。</p>}</div>
    <div id="replay-chart-legend" popover="auto" className="replay-chart-help" aria-label="图层与图例说明">
      <header className="replay-help-header"><h2 className="text-sm font-medium">图层与图例</h2><button type="button" popoverTarget="replay-chart-legend" popoverTargetAction="hide" className={controlClass} aria-label="关闭图例"><X size={15} aria-hidden /></button></header>
      <div className="replay-help-body">
        <fieldset className="replay-layer-options"><legend className="sr-only">显示图层</legend>{([['working', '在途委托'], ['canceled', '已撤销委托'], ['filled', '已全成委托'], ['market', '市场成交气泡'], ['execution', '账户成交标注']] as const).map(([key, label]) => <label key={key}><span className="relative inline-flex shrink-0 items-center justify-center"><input type="checkbox" checked={layers[key]} onChange={event => { setLayers({ ...layers, [key]: event.target.checked }); setHover(null) }} className="peer size-4 cursor-pointer appearance-none rounded-[3px] border border-ink-3 bg-transparent checked:border-accent checked:bg-accent-soft hover:border-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent forced-colors:appearance-auto" /><Check size={12} strokeWidth={2} aria-hidden="true" className="pointer-events-none absolute text-accent opacity-0 peer-checked:opacity-100 forced-colors:hidden" /></span>{label}</label>)}</fieldset>
        <p className="replay-help-note">只影响主图，委托按已录终态筛选。</p>
        <div className="replay-help-grid">
          <section><h3>盘口</h3><p>颜色越亮挂量越多，竖纹表示未知。</p><p>仅含已录档位，聚合取峰值；引导线对应真实价格。</p></section>
          <section><h3>委托</h3><p><span aria-hidden>● → △ / ○</span> 入场确认 → 全成 / 撤销</p><p>实线为本次，点线为账户上下文，长虚线为待撤。</p><p>轨迹不代表排队顺序。</p></section>
          <section><h3>成交</h3><p><span aria-hidden>━ ○</span> 白线和气泡表示市场成交。</p><p>账户成交见“成交”。</p></section>
          <section><h3>账户挂单</h3><p>账户挂单为当前时刻的未成交剩余量，包含其他执行，不代表持仓。</p></section>
        </div>
        <p className="replay-help-note">查看不改变游标，定位才切换时刻。未来记录灰显。</p>
      </div>
      <footer className="replay-help-footer">拖动平移 · 滚轮缩放 · 点击查看 · Esc 关闭</footer>
    </div>
  </section>
}
