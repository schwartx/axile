import { useCallback, useLayoutEffect, useMemo, useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { ArrowLeft, ArrowRight, Check, Circle, Crosshair, Info, Minus, RotateCcw, X } from 'lucide-react'
import { ValueSlider } from '@/components/ui/ValueSlider'
import { Tooltip } from '@/components/ui/Tooltip'
import { canvasLine, prepareCanvas, readCanvasTheme, type CanvasTheme } from '@/components/viz/chartCanvas'
import { centerReplayPrice, currentReplayQuote, replayChartLayout, replayHeatCells, replayMarketPriceSegments, replayOnBookOrder, replayPriceTicks, replayPriceY, replayQuoteEnd, replayRemainingQuantity, replayTimeX, replayXTime, replayYPrice, zoomReplayPrice, type ReplayChartLayout, type ReplayPriceView } from '@/features/replay/chartGeometry'
import { clampReplayView, replayOrderFocus, replayOrderTimeline, replayTradeKey, replayVisibleOrderMarks, replayVisibleOrders, zoomReplayPriceAt, zoomReplayTime } from '@/features/replay/chartInteraction'
import { replayBubbleRadius, replayMarketBubbles } from '@/features/replay/marketBubbles'
import { recordedDepth, replayBestLevel, replayObservedPriceView } from '@/features/replay/depthGeometry'
import { inspectReplayOrder, inspectReplayTrade, nearestReplayInspection, replayFutureRecord, type InspectionRecord } from '@/features/replay/inspection'
import { ReplayInspector } from '@/features/replay/ReplayInspector'
import { ReplayDepthLadder } from '@/features/replay/ReplayDepthLadder'
import { replayOrderOwnershipGroups, replayOwnership } from '@/features/replay/ownership'
import { replayInstrument, replayNumber, replayTime } from '@/features/replay/presentation'
import type { ReplayCursor, ReplayOrder, ReplayProjection, ReplayRange, ReplayWindow } from '@/features/replay/types'

interface Props {
  children?: ReactNode
  scene?: ReplayProjection
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

const controlClass = 'inline-flex min-h-7 min-w-7 cursor-pointer items-center justify-center rounded-chip text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-40'

function drawTimeAxis(ctx: CanvasRenderingContext2D, height: number, range: ReplayRange, layout: ReplayChartLayout, theme: CanvasTheme) {
  canvasLine(ctx, layout.left, height - 18, layout.right, height - 18, theme.line)
  ctx.fillStyle = theme.ink
  const duration = range.end_us - range.start_us
  const label = (time: number) => duration < 100_000 ? replayTime(time) : duration < 5_000_000 ? replayTime(time).slice(0, -3) : replayTime(time, false, false)
  const spacing = Math.max(145, ctx.measureText(label(range.start_us)).width * 1.5 + 12)
  const labels = Math.max(2, Math.floor((layout.right - layout.left) / spacing))
  for (let index = 0; index <= labels; index++) {
    const time = Math.min(range.end_us - 1, Math.round(range.start_us + (range.end_us - range.start_us) * index / labels))
    ctx.textAlign = index === 0 ? 'left' : index === labels ? 'right' : 'center'
    ctx.fillText(label(time), replayTimeX(time, range, layout), height - 3)
  }
  ctx.textAlign = 'left'
}

function drawOrderHistory(ctx: CanvasRenderingContext2D, symbol: string, history: ReplayOrder[], projection: ReplayProjection, range: ReplayRange, view: ReplayPriceView, layout: ReplayChartLayout, selected: string | null, executionId: string, theme: CanvasTheme, cursor: ReplayCursor, hovered: string | undefined = undefined) {
  const { segments, marks } = replayOrderTimeline(history, symbol, range, projection)
  const color = (order: ReplayOrder) => replayFutureRecord(order.time_us, order.capture_seq, cursor) ? theme.ink : order.cancel_pending ? theme.warn : replayOwnership(order, executionId).kind === 'current' ? theme.accent : theme.ink
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
  for (const { order, kind, time, price } of replayVisibleOrderMarks(marks, range, view, layout)) {
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
export function ReplayChart({ window: replayWindow, range, projection, symbol, executionId, selectedOrder, onSelectOrder, onSeek, selectedTrade, onSelectTrade, onInspect, firstQuoteControl, symbolControl, children, scene = projection }: Props) {
  const holder = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null)
  const [size, setSize] = useState({ width: 900, height: 300, revision: 0 })
  const [manualView, setManualView] = useState<ReplayPriceView | null>(null)
  const [timeView, setTimeView] = useState<ReplayRange | null>(null)
  const visibleRange = timeView ?? range
  const [layers, setLayers] = useState({ working: true, canceled: true, filled: true, market: true, execution: true })
  const recorded = scene
  const drawThrough = recorded.time_us
  const cursorInside = projection.time_us >= visibleRange.start_us && projection.time_us < visibleRange.end_us
  const [hover, setHover] = useState<(InspectionRecord & { cursor: string; x: number; y: number; records: InspectionRecord[];  }) | null>(null)
  const [inspection, setInspection] = useState<{ record: InspectionRecord; records: InspectionRecord[]; x: number; y: number } | null>(null)
  const gesture = useRef<{ id: number; x: number; y: number; view: ReplayPriceView; range: ReplayRange; mode: 'chart' | 'price' | 'time' | 'seek'; moved: boolean } | null>(null)
  const instrument = replayInstrument(replayWindow.context, symbol)
  const orders = useMemo(() => Object.values(projection.orders).filter(order => order.symbol === symbol), [projection.orders, symbol])
  const selected = Object.values(scene.orders).find(order => order.key === selectedOrder)
  const currentQuote = useMemo(() => currentReplayQuote(projection.quotes[symbol], projection.time_us, replayWindow.gaps), [projection.quotes, symbol, projection.time_us, replayWindow.gaps])
  const historyQuotes = useMemo(() => recorded.quoteHistory.filter(quote => quote.symbol === symbol && quote.time_us <= drawThrough), [recorded.quoteHistory, symbol, drawThrough])
  const historyOrders = useMemo(() => recorded.orderHistory.filter(order => order.symbol === symbol && order.time_us <= drawThrough), [recorded.orderHistory, symbol, drawThrough])
  const firstQuote = historyQuotes.find(quote => currentReplayQuote(quote, quote.time_us, replayWindow.gaps))
  const futureDepth = Boolean(firstQuote && replayFutureRecord(firstQuote.time_us, firstQuote.capture_seq, projection))
  const displayQuote = currentQuote ?? (futureDepth ? firstQuote! : null)
  const depthLabel = displayQuote ? `${futureDepth ? '未到达 · ' : ''}已录买 ${displayQuote.bids.length} / 卖 ${displayQuote.asks.length} 档` : '盘口未记录／已失效'
  const visibleOrders = useMemo(() => replayVisibleOrders(historyOrders, layers), [historyOrders, layers])
  const marketSegments = useMemo(() => replayWindow.context.streams?.[symbol]?.market_trade?.availability === 'unavailable' ? [] : replayMarketPriceSegments(recorded.marketTrades, symbol, visibleRange, drawThrough, replayWindow.gaps), [replayWindow, recorded.marketTrades, drawThrough, symbol, visibleRange])
  const initialView = useMemo(() => replayObservedPriceView(historyQuotes, historyOrders, recorded.marketTrades.filter(trade => trade.symbol === symbol), instrument?.price_tick), [historyQuotes, historyOrders, recorded.marketTrades, symbol, instrument?.price_tick])
  const view = manualView ?? initialView
  const hoverCursor = `${replayWindow.window_id}:${symbol}:${projection.time_us}:${projection.capture_seq}:${visibleRange.start_us}:${visibleRange.end_us}:${view?.min}:${view?.max}`
  const activeHover = hover?.cursor === hoverCursor ? hover : null
  const selectedExecution = layers.execution && replayWindow.context.streams?.[symbol]?.own_trade?.availability !== 'unavailable' ? recorded.ownTrades.find(trade => replayTradeKey(trade) === selectedTrade && trade.symbol === symbol && trade.time_us >= visibleRange.start_us && trade.time_us < visibleRange.end_us && (!historyOrders.some(order => order.order_id === trade.order_id) || visibleOrders.some(order => order.order_id === trade.order_id))) : undefined
  const height = size.height
  const [priceAxisWidth, setPriceAxisWidth] = useState(76)
  const layout = useMemo(() => ({ ...replayChartLayout(size.width, height, priceAxisWidth), top: selectedExecution ? 28 : 0 }), [size.width, height, priceAxisWidth, selectedExecution])
  const inspectionWidth = Math.min(320, layout.right - 16)
  const priceTicks = useMemo(() => view ? replayPriceTicks(view, layout.bottom - layout.top) : [], [view, layout.bottom, layout.top])
  const bubbles = useMemo(() => !layers.market || replayWindow.context.streams?.[symbol]?.market_trade?.availability === 'unavailable' ? [] : replayMarketBubbles(recorded.marketTrades, symbol, visibleRange, layout, projection, replayWindow.gaps).filter(bubble => !view || bubble.price >= view.min && bubble.price <= view.max), [layers.market, replayWindow, recorded.marketTrades, symbol, visibleRange, layout, projection, view])
  const maxBubble = bubbles.reduce((max, bubble) => Math.max(max, bubble.quantity), 1)
  const ownOrders = useMemo(() => orders.filter(replayOnBookOrder), [orders])
  const depthOrders = futureDepth ? [] : ownOrders
  const decimals = instrument?.price_decimals ?? 6
  const priceText = useCallback((price: number | null) => replayNumber(price, decimals), [decimals])
  const quantityText = useCallback((quantity: number | null) => replayNumber(quantity, instrument?.quantity_decimals ?? 6), [instrument?.quantity_decimals])
  const heat = useMemo(() => view ? replayHeatCells(historyQuotes, visibleRange, drawThrough, view, layout, replayWindow.gaps) : null, [historyQuotes, visibleRange, drawThrough, view, layout, replayWindow.gaps])
  const maxHeat = heat?.cells.reduce((maximum, cell) => Math.max(maximum, cell.quantity ?? 0), 0) ?? 0

  useLayoutEffect(() => { setManualView(null); setTimeView(null); setHover(null); setInspection(null); gesture.current = null }, [replayWindow.window_id, symbol])
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
    if (!canvas.current || !holder.current) return
    const ctx = prepareCanvas(canvas.current, size.width, height)
    if (!ctx) return
    const theme = readCanvasTheme(holder.current), css = getComputedStyle(holder.current)
    ctx.font = `12px ${theme.font}`; ctx.fillStyle = theme.surface; ctx.fillRect(0, 0, size.width, height)
    ctx.fillStyle = theme.ink
    const measuredAxisWidth = Math.max(76, Math.ceil(Math.max(0, ...priceTicks.map(price => ctx.measureText(priceText(price)).width))) + 16)
    if (measuredAxisWidth !== priceAxisWidth) { setPriceAxisWidth(measuredAxisWidth); return }
    if (!view || !heat) { ctx.fillText('这段记录尚无有效价位。', layout.left, height / 2); drawTimeAxis(ctx, height, visibleRange, layout, theme); return }
    const cursorX = replayTimeX(projection.time_us, visibleRange, layout)
    const splitX = Math.max(layout.left, Math.min(layout.right, cursorX))
    for (const future of [false, true]) {
      const paint = future ? { ...theme, accent: theme.ink, warn: theme.ink } : theme
      const up = future ? theme.ink : css.getPropertyValue('--color-up').trim()
      const down = future ? theme.ink : css.getPropertyValue('--color-down').trim()
      ctx.save(); ctx.beginPath(); ctx.rect(future ? splitX : layout.left, layout.top, future ? layout.right - splitX : splitX - layout.left, layout.bottom - layout.top); ctx.clip()
      const maxQuantity = Math.max(1, maxHeat)
      ctx.save(); ctx.beginPath(); ctx.rect(layout.left, layout.top, Math.max(0, replayTimeX(drawThrough, visibleRange, layout) - layout.left), layout.bottom - layout.top); ctx.clip()
      ctx.fillStyle = paint.accent
      for (const cell of heat.cells) {
        ctx.globalAlpha = cell.quantity == null ? 0.12 : cell.quantity === 0 ? 0.025 : 0.06 + Math.sqrt(cell.quantity / maxQuantity) * 0.57
        ctx.fillRect(cell.x, cell.y - heat.rowHeight / 2, heat.columnWidth, heat.rowHeight)
        if (cell.unknown) { ctx.globalAlpha = 0.5; ctx.fillRect(cell.x, cell.y - heat.rowHeight / 2, 1, heat.rowHeight) }
      }
      ctx.globalAlpha = 1
      ctx.restore()
      for (const gap of replayWindow.gaps) {
        if ((gap.symbol && gap.symbol !== symbol) || gap.start_us > drawThrough || (gap.topic && gap.topic !== 'depth' && !gap.topic.startsWith('/quote'))) continue
        const x = replayTimeX(Math.max(visibleRange.start_us, gap.start_us), visibleRange, layout), end = replayTimeX(Math.min(drawThrough, gap.end_us), visibleRange, layout)
        ctx.fillStyle = theme.fill; ctx.fillRect(x, layout.top, Math.max(1, end - x), layout.bottom - layout.top)
      }
      for (const side of ['买', '卖'] as const) {
        ctx.strokeStyle = side === '买' ? up : down; ctx.lineWidth = 1.3
        historyQuotes.forEach((quote, index) => {
          const best = replayBestLevel(quote, side), next = historyQuotes[index + 1]
          if (!best || !currentReplayQuote(quote, quote.time_us, replayWindow.gaps)) return
          const end = replayQuoteEnd(quote, next?.time_us ?? drawThrough, drawThrough, replayWindow.gaps)
          if (end < visibleRange.start_us || quote.time_us >= visibleRange.end_us || (!quote.book_valid || !quote.available)) return
          const x = replayTimeX(Math.max(visibleRange.start_us, quote.time_us), visibleRange, layout), right = replayTimeX(Math.min(visibleRange.end_us, end), visibleRange, layout)
          const y = replayPriceY(best.price, view, layout)
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(Math.max(x + 1, right), y)
          const nextBest = next && currentReplayQuote(next, next.time_us, replayWindow.gaps) && replayBestLevel(next, side)
          if (nextBest && end === next.time_us) ctx.lineTo(right, replayPriceY(nextBest.price, view, layout))
          ctx.stroke()
        })
        ctx.lineWidth = 1
      }
      if (marketSegments.length) {
        ctx.strokeStyle = future ? theme.ink : css.getPropertyValue('--color-ink-1').trim(); ctx.lineWidth = 1.5
        for (const segment of marketSegments) {
          ctx.beginPath()
          segment.forEach((trade, index) => {
            const x = replayTimeX(trade.time_us, visibleRange, layout), y = replayPriceY(trade.price!, view, layout)
            if (index === 0) ctx.moveTo(x, y)
            else ctx.lineTo(x, y)
          })
          ctx.stroke()
        }
        ctx.lineWidth = 1
      }
      for (const bubble of bubbles) {
        ctx.fillStyle = ctx.strokeStyle = replayFutureRecord(bubble.time_us, bubble.trades[0].capture_seq, projection) ? theme.ink : bubble.direction === 'BUY' ? up : bubble.direction === 'SELL' ? down : theme.ink
        ctx.save(); ctx.shadowColor = theme.bg; ctx.shadowBlur = 3; ctx.shadowOffsetY = 2; ctx.globalAlpha = .85; ctx.beginPath()
        ctx.arc(replayTimeX(bubble.time_us, visibleRange, layout), replayPriceY(bubble.price, view, layout), replayBubbleRadius(bubble.quantity, maxBubble), 0, Math.PI * 2)
        const stable = bubble.trades.every(trade => trade.identity_quality === 'stable' && !trade.conflict)
        if (stable) ctx.fill()
        ctx.shadowBlur = 0; ctx.globalAlpha = 1
        const hovered = activeHover?.object === `bubble:${bubble.trades[0].capture_seq}:${bubble.price}:${bubble.direction}`
        if (hovered) ctx.strokeStyle = replayFutureRecord(bubble.time_us, bubble.trades[0].capture_seq, projection) ? theme.ink : paint.accent
        else if (stable) ctx.strokeStyle = theme.surface
        ctx.lineWidth = hovered ? 2 : 1.5; ctx.stroke(); ctx.restore()
      }
      ctx.globalAlpha = 1
      drawOrderHistory(ctx, symbol, visibleOrders, recorded, visibleRange, view, layout, selectedOrder, executionId, paint, projection, activeHover?.object)
      ctx.restore()
    }
    if (cursorInside) canvasLine(ctx, cursorX, layout.top, cursorX, height - 18, theme.accent)
    if (selectedExecution) {
      const future = replayFutureRecord(selectedExecution.time_us, selectedExecution.capture_seq, projection)
      ctx.fillStyle = future ? theme.ink : theme.accent
      ctx.fillText(`${future ? '未到达 · ' : ''}选中逐笔 ${replayTime(selectedExecution.time_us)} · ${quantityText(selectedExecution.quantity)} @ ${priceText(selectedExecution.price)}`, layout.left, 18, layout.right - layout.left)
    }
    for (const price of priceTicks) {
      const y = replayPriceY(price, view, layout)
      canvasLine(ctx, layout.left, y, layout.right, y, theme.line)
      ctx.fillStyle = theme.ink; ctx.textAlign = 'right'; ctx.fillText(priceText(price), layout.priceX, Math.max(12, Math.min(layout.bottom, y + 4))); ctx.textAlign = 'left'
    }
    drawTimeAxis(ctx, height, visibleRange, layout, theme)
  }, [priceAxisWidth, priceTicks, size, height, projection, replayWindow, visibleRange, symbol, executionId, selectedOrder, selectedExecution, view, layout, historyQuotes, visibleOrders, marketSegments, priceText, quantityText, heat, maxHeat, recorded, drawThrough, cursorInside, bubbles, maxBubble, activeHover?.object])

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
  const timeline = useMemo(() => replayOrderTimeline(visibleOrders, symbol, visibleRange, recorded), [visibleOrders, symbol, visibleRange, recorded])
  const inspectAt = (x: number, y: number, select = false) => {
    if (!view || y < layout.top || y > layout.bottom || x < layout.left || x > layout.right && x <= layout.priceX + 8) { setHover(null); if (select) setInspection(null); return }
    const time = x > layout.right ? projection.time_us : replayXTime(x, visibleRange, layout)
    if (time > drawThrough) { setHover(null); if (select) setInspection(null); return }
    const price = replayYPrice(y, view, layout)
    const format = { executionId, cursor: projection, unit: instrument.quantity_label ?? '', price: priceText, quantity: quantityText }
    let records: InspectionRecord[] = [], candidates: InspectionRecord[] = []
    if (x <= layout.right) {
      const visibleMarks = replayVisibleOrderMarks(timeline.marks, visibleRange, view, layout)
      candidates = visibleMarks.map(mark => inspectReplayOrder(mark.order, format, replayTimeX(mark.time, visibleRange, layout), replayPriceY(mark.price, view, layout)))
        .filter(record => Math.hypot(record.x - x, record.y - y) <= 18)
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
        const object = `bubble:${bubble.trades[0].capture_seq}:${bubble.price}:${bubble.direction}`
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
    const quote = x > layout.right ? currentQuote : currentReplayQuote(historyQuotes.findLast(item => item.time_us <= time), time, replayWindow.gaps)
    const level = quote && recordedDepth(quote).find(item => Math.abs(replayPriceY(item.price, view, layout) - y) <= Math.max(3, (layout.bottom - layout.top) * view.tick / (view.max - view.min) / 2))
    const fallback: InspectionRecord = { id: level ? 'depth' : 'pointer', object: 'depth', kind: 'depth', title: level ? `${x > layout.right ? '游标盘口' : '历史盘口'} · ${level.side}${level.rank ?? '·0'}` : '指针价格',
      headline: priceText(level?.price ?? Math.round(price / (instrument.price_tick ?? 10 ** -decimals)) * (instrument.price_tick ?? 10 ** -decimals)), status: level ? `挂量 ${quantityText(level.quantity)} ${format.unit}` : '挂量未知／未覆盖', label: '盘口观察', raw: '仅表示该时刻已录到的档位；档位不代表排队顺序。',
      time, future: replayFutureRecord(time, 0, projection), x, y: level ? replayPriceY(level.price, view, layout) : y }
    const hit = nearestReplayInspection(candidates, x, y, activeHover?.id) ?? fallback
    const next = { cursor: hoverCursor, ...hit, records: [hit, ...records.filter(record => record.id !== hit.id)] }
    setHover(next)
    if (select) {
      if (next.id === 'pointer') setInspection(null)
      else { onInspect(); setInspection({ record: next, records: next.records, x, y }) }
    }
    return next
  }
  const pointerPosition = (event: PointerEvent<HTMLCanvasElement>) => { const bounds = event.currentTarget.getBoundingClientRect(); return { x: event.clientX - bounds.left, y: event.clientY - bounds.top } }
  const pointerMove = (event: PointerEvent<HTMLCanvasElement>) => {
    const { x, y } = pointerPosition(event), drag = gesture.current
    event.currentTarget.style.cursor = view && y >= layout.top && x > layout.right && x <= layout.priceX + 8 ? 'ns-resize' : 'crosshair'
    if (!drag) { inspectAt(x, y); return }
    const dx = x - drag.x, dy = y - drag.y
    if (!drag.moved && Math.hypot(dx, dy) < 3) return
    drag.moved = true; setHover(null); setInspection(null)
    if (drag.mode === 'seek') { onSeek(replayXTime(x, drag.range, layout)); return }
    if (drag.mode === 'price') setManualView(zoomReplayPriceAt(drag.view, Math.exp(dy / 160), replayYPrice(drag.y, drag.view, layout)))
    else if (drag.mode === 'time') setTimeView(zoomReplayTime(drag.range, Math.exp(dx / 200), replayXTime(drag.x, drag.range, layout), range))
    else {
      const shift = -dx / (layout.right - layout.left) * (drag.range.end_us - drag.range.start_us)
      setTimeView(clampReplayView({ start_us: drag.range.start_us + shift, end_us: drag.range.end_us + shift }, range))
      const priceShift = dy / (layout.bottom - layout.top) * (drag.view.max - drag.view.min)
      setManualView({ ...drag.view, min: drag.view.min + priceShift, max: drag.view.max + priceShift })
    }
  }
  useLayoutEffect(() => {
    const element = canvas.current
    if (!element) return
    const wheel = (event: WheelEvent) => {
      if (!view) return
      const bounds = element.getBoundingClientRect(), x = event.clientX - bounds.left, y = event.clientY - bounds.top
      if (x < layout.left || x > layout.priceX + 8 || y < layout.top) return
      event.preventDefault(); setHover(null); setInspection(null)
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
    <div className="replay-chart-meta" style={{ gridTemplateColumns: layout.columns ? `minmax(0, ${layout.priceX - 8}px) minmax(0, 1fr)` : undefined }}><div className="replay-chart-meta-main" style={{ paddingRight: Math.max(0, layout.priceX + 4 - layout.right) }}><div className="replay-chart-legend"><span className="replay-depth-bid"><Minus size={14} aria-hidden />买一</span><span className="replay-depth-ask"><Minus size={14} aria-hidden />卖一</span><span><Minus size={14} aria-hidden />成交价</span>{layers.market && <span><Circle size={10} aria-hidden />市场成交</span>}<span className="text-accent"><Minus size={14} aria-hidden />账户委托</span><span><Minus size={14} aria-hidden />未到达</span></div>{activeHover && activeHover.id !== 'pointer' && <span className="replay-hover-readout num" aria-label="悬停读数">{activeHover.future ? '未到达 · ' : ''}{activeHover.title} · {activeHover.headline} · {activeHover.status}</span>}      <div className="replay-chart-view-controls replay-chart-navigation" role="group" aria-label="图表导航">
      <div className="replay-chart-return-slot">      {!cursorInside && <div className="replay-chart-return">
        <Tooltip content={`游标在${projection.time_us < visibleRange.start_us ? '左' : '右'}侧 · 回到游标`}><button type="button" className={controlClass} aria-label="回到游标" onClick={() => { const span = visibleRange.end_us - visibleRange.start_us; setTimeView(clampReplayView({ start_us: projection.time_us - span / 2, end_us: projection.time_us + span / 2 }, range)) }}>{projection.time_us < visibleRange.start_us ? <ArrowLeft size={15} aria-hidden /> : <ArrowRight size={15} aria-hidden />}</button></Tooltip>
      </div>}
      </div>
        {firstQuoteControl}
        <Tooltip content="恢复视图"><button type="button" className={controlClass} onClick={resetView} disabled={!manualView && !timeView} aria-label="恢复视图"><RotateCcw size={15} aria-hidden /></button></Tooltip>
      </div>
    </div><div className="replay-depth-status"><span className="replay-depth-fit-slot"><span style={{ visibility: outsideDepth > 0 ? 'hidden' : 'visible' }}>{depthLabel}</span><Tooltip content={`当前视野外有 ${outsideDepth} 个已录档位`}><button type="button" className="text-accent focus-visible:outline-2 focus-visible:outline-accent" style={{ visibility: outsideDepth > 0 ? 'visible' : 'hidden' }} disabled={outsideDepth === 0} onClick={() => setManualView(null)}>显示全部档位</button></Tooltip></span><span className="replay-heat-scale"><i aria-hidden />{heat?.cells.some(cell => cell.quantity != null) ? `挂量 0 → ${quantityText(maxHeat)}` : '挂量尚未知'}</span></div></div>
    <div ref={holder} className="replay-chart-plot">
      <p id="replay-scene-summary" className="sr-only">已读取时段，游标之后的记录灰显为未到达，已录自有成交 {recorded.ownTrades.filter(trade => trade.symbol === symbol && trade.time_us >= visibleRange.start_us && trade.time_us < visibleRange.end_us).length} 笔。盘口与委托状态对应游标 {replayTime(projection.time_us)}。</p>
      <canvas ref={canvas} style={{ height }} className="block w-full touch-none cursor-crosshair focus-visible:outline-2 focus-visible:outline-accent" tabIndex={0}
        aria-describedby="replay-scene-summary"
        aria-label="历史盘口热力图与共享价格轴；拖动浏览，滚轮缩放，方向键平移，正负键缩放，Home 复位" role="group"
        onPointerDown={event => { if (!view || event.button !== 0) return; const { x, y } = pointerPosition(event); if (y < layout.top) return; if (x < layout.left || x > layout.priceX + 8) return; event.currentTarget.focus(); event.currentTarget.setPointerCapture(event.pointerId); gesture.current = { id: event.pointerId, x, y, view, range: visibleRange, mode: x > layout.right ? 'price' : cursorInside && Math.abs(x - replayTimeX(projection.time_us, visibleRange, layout)) <= 8 ? 'seek' : y > layout.bottom ? 'time' : 'chart', moved: false } }}
        onPointerMove={pointerMove} onPointerLeave={() => { if (!gesture.current) setHover(null) }}
        onPointerUp={event => { const drag = gesture.current; gesture.current = null; if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); if (drag && !drag.moved) { const { x, y } = pointerPosition(event); if (x >= layout.left && x <= layout.right) inspectAt(x, y, true) } }}
        onPointerCancel={() => { gesture.current = null; setHover(null) }} onLostPointerCapture={() => { gesture.current = null }}
        onKeyDown={event => {
          if (event.key.startsWith('Arrow')) { event.preventDefault(); pan(event.key === 'ArrowLeft' ? -0.15 : event.key === 'ArrowRight' ? 0.15 : 0, event.key === 'ArrowUp' ? 0.15 : event.key === 'ArrowDown' ? -0.15 : 0) }
          else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(0.7) }
          else if (event.key === '-') { event.preventDefault(); zoom(1.4) }
          else if (event.key === 'Home') { event.preventDefault(); resetView() }
          else if (event.key === 'Enter' && view) { event.preventDefault(); inspectAt(replayTimeX(projection.time_us, visibleRange, layout), replayPriceY(selected?.price ?? (view.min + view.max) / 2, view, layout), true) }
          else if (event.key === 'Escape') { setHover(null); setInspection(null) }
        }} />
      <div className="replay-chart-view-controls replay-chart-tools" role="group" aria-label="图表工具" style={{ left: layout.left + 8, top: layout.top + 8 }}>
        <Tooltip content="聚焦委托过程"><button type="button" className={controlClass} aria-label="聚焦委托过程" disabled={!view || !selected} onClick={focusOrder}><Crosshair size={14} aria-hidden /></button></Tooltip>
        <Tooltip content="图层与图例说明"><button type="button" className={controlClass} popoverTarget="replay-chart-legend" aria-label="图层与图例说明"><Info size={15} aria-hidden /></button></Tooltip>
      </div>
      <div className="replay-chart-axis" data-outside={!cursorInside || undefined} style={{ left: layout.left - 12, right: size.width - layout.right - 12, top: height - 30 }}>
        <ValueSlider variant="axis" label="录制定位" min={0} max={Math.max(1, visibleRange.end_us - visibleRange.start_us - 1)} step={1} value={projection.time_us - visibleRange.start_us}
          formatValue={offset => cursorInside ? replayTime(visibleRange.start_us + offset) : `游标在当前视野之外，${replayTime(projection.time_us)}`} onChange={offset => { setHover(null); onSeek(visibleRange.start_us + offset) }} />
      </div>
      {view && layout.columns && <ReplayDepthLadder quote={displayQuote} future={futureDepth} orders={depthOrders} view={view} layout={layout} height={height} executionId={executionId} selected={selectedOrder} onSelect={key => { onInspect(); onSelectOrder(key) }} onZoom={setManualView} priceText={priceText} quantityText={quantityText} />}
      {inspection && <div className="replay-inspection-float" aria-label="图中记录浮卡" style={{
        width: inspectionWidth,
        left: Math.max(8, Math.min(layout.right - inspectionWidth - 8, inspection.x + 12 + inspectionWidth <= layout.right ? inspection.x + 12 : inspection.x - 12 - inspectionWidth)),
        ...(inspection.y <= height / 2 ? { top: inspection.y + 12 } : { bottom: height - inspection.y + 12 }),
        maxHeight: Math.max(0, (inspection.y <= height / 2 ? height - inspection.y : inspection.y) - 20),
      }}>
        <ReplayInspector record={inspection.record} records={inspection.records} onChoose={record => setInspection({ ...inspection, record })}
          onClose={() => { setInspection(null); canvas.current?.focus() }}
          onView={() => { if (inspection.record.trade) onSelectTrade(inspection.record.trade); else if (inspection.record.order) onSelectOrder(inspection.record.order); setInspection(null) }}
          onSeek={() => { onSeek(inspection.record.time, inspection.record.seq); setInspection(null) }} />
      </div>}
      {activeHover && <>
        {activeHover.x <= layout.right && <div className="replay-crosshair" aria-hidden style={{ left: activeHover.x, top: layout.top, height: layout.bottom - layout.top }} />}
        <div className="replay-crosshair-horizontal" aria-hidden style={{ left: layout.left, right: 6, top: activeHover.y }} />
        <span className="replay-crosshair-price num" style={{ left: layout.right + 2, width: layout.priceX - layout.right + 6, top: Math.max(0, Math.min(layout.bottom - 20, activeHover.y - 10)) }}>{view && priceText(Math.round(replayYPrice(activeHover.y, view, layout) / (instrument.price_tick ?? 10 ** -decimals)) * (instrument.price_tick ?? 10 ** -decimals))}</span>
        <time className="replay-crosshair-time num" style={{ left: Math.max(layout.left, Math.min(layout.right - 140, activeHover.x - 70)), top: height - 24 }}>{replayTime(activeHover.time)}</time>
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
          <section><h3>盘口</h3><p>颜色越深挂量越多，竖纹表示未知。</p><p>仅含已录档位，聚合取峰值；引导线对应真实价格。</p></section>
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
