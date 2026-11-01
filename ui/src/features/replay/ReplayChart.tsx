import { useCallback, useLayoutEffect, useMemo, useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { Circle, Crosshair, Diamond, Info, Minus, Pin, Plus, RotateCcw, Square, X } from 'lucide-react'
import { ValueSlider } from '@/components/ui/ValueSlider'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
import { withViewTransition } from '@/lib/viewTransition'
import { canvasLine, prepareCanvas, readCanvasTheme, type CanvasTheme } from '@/components/viz/chartCanvas'
import { centerReplayPrice, currentReplayQuote, replayChartLayout, replayHeatCells, replayMarketPriceSegments, replayOnBookOrder, replayPriceY, replayQuoteEnd, replayRemainingQuantity, replayTimeX, replayXTime, replayYPrice, zoomReplayPrice, type ReplayChartLayout, type ReplayPriceView } from '@/features/replay/chartGeometry'
import { clampReplayView, replayOrderCommands, replayOrderFocus, replayOrderTimeline, replayTradeKey, zoomReplayPriceAt, zoomReplayTime } from '@/features/replay/chartInteraction'
import { recordedDepth, replayBestLevel, replayObservedPriceView, replayTradeLiquidity } from '@/features/replay/depthGeometry'
import { ReplayDepthLadder } from '@/features/replay/ReplayDepthLadder'
import { replayOrderOwnershipGroups, replayOwnership } from '@/features/replay/ownership'
import { replayInstrument, replayNumber, replayTime, replayStatusLabels } from '@/features/replay/presentation'
import type { ReplayOrder, ReplayProjection, ReplayRange, ReplayTrade, ReplayWindow } from '@/features/replay/types'

interface Props {
  overviewLabel?: string
  children?: ReactNode
  scene?: ReplayProjection
  symbolControl?: ReactNode
  window: ReplayWindow
  range: ReplayRange
  projection: ReplayProjection
  symbol: string
  executionId: string
  selectedOrder: string | null
  onSelectOrder: (key: string) => void
  onSeek: (timeUs: number) => void
  selectedTrade: string | null
  onSelectTrade: (key: string) => void
  onInspect: () => void
}

const controlClass = 'inline-flex min-h-7 min-w-7 cursor-pointer items-center justify-center rounded-chip text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-40'

function drawTimeAxis(ctx: CanvasRenderingContext2D, height: number, range: ReplayRange, layout: ReplayChartLayout, theme: CanvasTheme) {
  canvasLine(ctx, layout.left, height - 18, layout.right, height - 18, theme.line)
  ctx.fillStyle = theme.ink
  const labels = Math.max(2, Math.floor((layout.right - layout.left) / 145))
  for (let index = 0; index <= labels; index++) {
    const time = Math.min(range.end_us - 1, Math.round(range.start_us + (range.end_us - range.start_us) * index / labels))
    ctx.textAlign = index === 0 ? 'left' : index === labels ? 'right' : 'center'
    const duration = range.end_us - range.start_us
    ctx.fillText(duration < 100_000 ? replayTime(time) : duration < 5_000_000 ? replayTime(time).slice(0, -3) : replayTime(time, false, false), replayTimeX(time, range, layout), height - 3)
  }
  ctx.textAlign = 'left'
}

function drawTrade(ctx: CanvasRenderingContext2D, trade: ReplayTrade, own: 'current' | 'context' | false, x: number, y: number, color: string) {
  const radius = Math.min(11, Math.max(3, Math.sqrt(trade.quantity ?? 0) * 1.8))
  ctx.fillStyle = ctx.strokeStyle = color
  ctx.globalAlpha = own ? 1 : 0.62
  ctx.beginPath()
  if (own === 'current') { ctx.moveTo(x, y - radius); ctx.lineTo(x + radius, y); ctx.lineTo(x, y + radius); ctx.lineTo(x - radius, y); ctx.closePath() }
  else if (own === 'context') ctx.rect(x - radius, y - radius, radius * 2, radius * 2)
  else ctx.arc(x, y, radius, 0, Math.PI * 2)
  if (trade.identity_quality === 'stable') ctx.fill()
  else ctx.stroke()
  ctx.globalAlpha = 1
}

function drawOrderHistory(ctx: CanvasRenderingContext2D, symbol: string, history: ReplayOrder[], projection: ReplayProjection, range: ReplayRange, view: ReplayPriceView, layout: ReplayChartLayout, selected: string | null, executionId: string, theme: CanvasTheme, priceText: (price: number | null) => string, quantityText: (quantity: number | null) => string, overview = false) {
  const { segments, marks } = replayOrderTimeline(history, symbol, range, projection)
  const color = (order: ReplayOrder) => order.cancel_pending ? theme.warn : replayOwnership(order, executionId).kind === 'current' ? theme.accent : theme.ink
  for (const { order, start, end, next } of segments) {
    if (order.price == null) continue
    const x = replayTimeX(start, range, layout), right = replayTimeX(end, range, layout), y = replayPriceY(order.price, view, layout)
    ctx.strokeStyle = color(order); ctx.lineWidth = order.key === selected ? 3 : 1.5
    ctx.globalAlpha = selected && order.key !== selected ? 0.45 : 1
    ctx.setLineDash(order.cancel_pending ? [5, 3] : replayOwnership(order, executionId).kind === 'current' ? [] : [2, 3])
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(Math.max(x + 1, right), y); ctx.stroke(); ctx.setLineDash([])
    if (order.carry_in && start === range.start_us) { ctx.beginPath(); ctx.moveTo(x + 5, y - 3); ctx.lineTo(x, y); ctx.lineTo(x + 5, y + 3); ctx.stroke() }
    if (next && replayOnBookOrder(next) && next.price != null && next.price !== order.price && next.time_us === end) {
      ctx.beginPath(); ctx.moveTo(right, y); ctx.lineTo(right, replayPriceY(next.price, view, layout)); ctx.stroke()
    }
  }
  for (const { order, kind, time, price } of marks) {
    const x = replayTimeX(time, range, layout), y = replayPriceY(price, view, layout), r = order.key === selected ? 5 : 4
    ctx.globalAlpha = selected && order.key !== selected ? 0.45 : 1
    ctx.strokeStyle = ctx.fillStyle = color(order); ctx.lineWidth = 1.8; ctx.beginPath()
    if (kind === 'placed') ctx.arc(x, y, r, 0, Math.PI * 2)
    else if (kind === 'changed') ctx.rect(x - r, y - r, r * 2, r * 2)
    else if (kind === 'filled') { ctx.moveTo(x - r, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x - r, y + r); ctx.closePath(); ctx.fill() }
    else { ctx.moveTo(x - r, y - r); ctx.lineTo(x + r, y + r); ctx.moveTo(x + r, y - r); ctx.lineTo(x - r, y + r) }
    ctx.stroke()
  }
  ctx.globalAlpha = 1
  const last = [...segments.map(segment => ({ order: segment.order, time: segment.end })), ...marks.map(mark => ({ order: mark.order, time: mark.time }))]
    .filter(item => item.order.key === selected && item.order.price != null).sort((a, b) => b.time - a.time || b.order.capture_seq - a.order.capture_seq)[0]
  if (last && last.order.price! >= view.min && last.order.price! <= view.max) {
    const { order, time } = last
    const text = `${overview ? '已录轨迹 · ' : ''}${order.direction === 'BUY' ? '买' : order.direction === 'SELL' ? '卖' : '委托'} ${quantityText(order.volume)} @ ${priceText(order.price)} · ${order.cancel_pending ? '撤单待确认' : replayStatusLabels[order.status ?? 'unknown'] ?? '状态未知'}`
    const width = Math.min(layout.right - layout.left - 16, ctx.measureText(text).width)
    const x = Math.max(layout.left + 8, Math.min(replayTimeX(time, range, layout) - width - 8, layout.right - width - 8))
    const y = replayPriceY(order.price!, view, layout), labelY = y < layout.top + 24 ? y + 20 : y - 12
    ctx.fillStyle = theme.surface; ctx.fillRect(x - 4, labelY - 13, width + 8, 18)
    ctx.fillStyle = color(order); ctx.fillText(text, x, labelY, width)
  }
  ctx.lineWidth = 1
}

/** 同一画布中的历史深度、COB 与 ORD 只共享一份价位映射。 */
export function ReplayChart({ window: replayWindow, range, projection, symbol, executionId, selectedOrder, onSelectOrder, onSeek, selectedTrade, onSelectTrade, onInspect, symbolControl, children, scene = projection, overviewLabel = '整段复盘' }: Props) {
  const holder = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null)
  const [size, setSize] = useState({ width: 900, height: 300, revision: 0 })
  const [manualView, setManualView] = useState<ReplayPriceView | null>(null)
  const [timeView, setTimeView] = useState<ReplayRange | null>(null)
  const visibleRange = timeView ?? range
  const [mode, setMode] = useState<'review' | 'step'>('review')
  const recorded = mode === 'review' ? scene : projection
  const drawThrough = recorded.time_us
  const cursorInside = projection.time_us >= visibleRange.start_us && projection.time_us < visibleRange.end_us
  const [hover, setHover] = useState<{ cursor: string; text: string; x: number; y: number; time: number; order?: string; trade?: string; pinned?: boolean } | null>(null)
  const hoverCloseTimer = useRef<number | undefined>(undefined)
  const keepHover = () => { window.clearTimeout(hoverCloseTimer.current); hoverCloseTimer.current = undefined }
  const leaveHover = () => {
    keepHover()
    hoverCloseTimer.current = window.setTimeout(() => { hoverCloseTimer.current = undefined; setHover(current => current?.pinned ? current : null) }, 200)
  }
  useLayoutEffect(() => () => window.clearTimeout(hoverCloseTimer.current), [])
  const gesture = useRef<{ id: number; x: number; y: number; view: ReplayPriceView; range: ReplayRange; mode: 'chart' | 'price' | 'time' | 'seek'; moved: boolean } | null>(null)
  const instrument = replayInstrument(replayWindow.context, symbol)
  const orders = useMemo(() => Object.values(projection.orders).filter(order => order.symbol === symbol), [projection.orders, symbol])
  const selected = Object.values(scene.orders).find(order => order.key === selectedOrder)
  const currentQuote = useMemo(() => currentReplayQuote(projection.quotes[symbol], projection.time_us, replayWindow.gaps), [projection.quotes, symbol, projection.time_us, replayWindow.gaps])
  const historyQuotes = useMemo(() => recorded.quoteHistory.filter(quote => quote.symbol === symbol && quote.time_us <= drawThrough), [recorded.quoteHistory, symbol, drawThrough])
  const historyOrders = useMemo(() => recorded.orderHistory.filter(order => order.symbol === symbol && order.time_us <= drawThrough), [recorded.orderHistory, symbol, drawThrough])
  const marketSegments = useMemo(() => replayWindow.context.streams?.[symbol]?.market_trade?.availability === 'unavailable' ? [] : replayMarketPriceSegments(recorded.marketTrades, symbol, visibleRange, drawThrough, replayWindow.gaps), [replayWindow, recorded.marketTrades, drawThrough, symbol, visibleRange])
  const initialView = useMemo(() => replayObservedPriceView(historyQuotes, historyOrders, recorded.marketTrades.filter(trade => trade.symbol === symbol), instrument?.price_tick), [historyQuotes, historyOrders, recorded.marketTrades, symbol, instrument?.price_tick])
  const view = manualView ?? initialView
  const hoverCursor = `${replayWindow.window_id}:${symbol}:${projection.time_us}:${projection.capture_seq}:${visibleRange.start_us}:${visibleRange.end_us}:${view?.min}:${view?.max}`
  const activeHover = hover?.cursor === hoverCursor ? hover : null
  const height = size.height
  const layout = useMemo(() => replayChartLayout(size.width, height), [size.width, height])
  const ownOrders = useMemo(() => orders.filter(replayOnBookOrder), [orders])
  const decimals = instrument?.price_decimals ?? 6
  const priceText = useCallback((price: number | null) => replayNumber(price, decimals), [decimals])
  const quantityText = useCallback((quantity: number | null) => replayNumber(quantity, instrument?.quantity_decimals ?? 6), [instrument?.quantity_decimals])
  const heat = useMemo(() => view ? replayHeatCells(historyQuotes, visibleRange, drawThrough, view, layout, replayWindow.gaps) : null, [historyQuotes, visibleRange, drawThrough, view, layout, replayWindow.gaps])
  const maxHeat = heat?.cells.reduce((maximum, cell) => Math.max(maximum, cell.quantity ?? 0), 0) ?? 0

  useLayoutEffect(() => { setManualView(null); setTimeView(null); setHover(null); gesture.current = null }, [replayWindow.window_id, symbol])
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
    const up = css.getPropertyValue('--color-up').trim(), down = css.getPropertyValue('--color-down').trim()
    ctx.font = `12px ${theme.font}`; ctx.fillStyle = theme.surface; ctx.fillRect(0, 0, size.width, height)
    ctx.fillStyle = theme.ink
    ctx.textAlign = 'right'; ctx.fillText('价格', layout.priceX, 18); ctx.textAlign = 'left'
    if (!view || !heat) { ctx.fillText(mode === 'step' ? '游标之前尚无有效价位。' : '这段记录尚无有效价位。', layout.left, height / 2); drawTimeAxis(ctx, height, visibleRange, layout, theme); return }
    const maxQuantity = Math.max(1, maxHeat)
    ctx.save(); ctx.beginPath(); ctx.rect(layout.left, layout.top, layout.right - layout.left, layout.bottom - layout.top); ctx.clip()
    ctx.save(); ctx.beginPath(); ctx.rect(layout.left, layout.top, Math.max(0, replayTimeX(drawThrough, visibleRange, layout) - layout.left), layout.bottom - layout.top); ctx.clip()
    ctx.fillStyle = theme.accent
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
        if (end < visibleRange.start_us || quote.time_us >= visibleRange.end_us || !quote.book_valid) return
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
      ctx.strokeStyle = css.getPropertyValue('--color-ink-1').trim(); ctx.lineWidth = 1.5
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
    drawOrderHistory(ctx, symbol, historyOrders, recorded, visibleRange, view, layout, selectedOrder, executionId, theme, priceText, quantityText, mode === 'review')
    if (selected) {
      ctx.strokeStyle = ctx.fillStyle = theme.warn
      for (const command of replayOrderCommands(selected, recorded)) {
        if (command.phase !== 'request' || command.price == null || command.operation !== 'place_order') continue
        const x = replayTimeX(command.time_us, visibleRange, layout), y = replayPriceY(command.price, view, layout)
        ctx.strokeRect(x - 4, y - 4, 8, 8)
        ctx.fillText('报单请求', x + 8, y < layout.top + 40 ? y + 28 : y - 28)
      }
    }
    const tradeGroups: [ReplayTrade[], boolean][] = [[recorded.marketTrades, false], [recorded.ownTrades, true]]
    for (const [trades, own] of tradeGroups) {
      const layer = replayWindow.context.streams?.[symbol]?.[own ? 'own_trade' : 'market_trade']
      if (layer?.availability === 'unavailable') continue
      for (const trade of trades) {
        if (trade.symbol !== symbol || trade.time_us < visibleRange.start_us || trade.time_us >= visibleRange.end_us || trade.time_us > drawThrough || trade.price == null || trade.quantity == null || trade.quantity <= 0) continue
        const current = own && replayOwnership(trade, executionId).kind === 'current'
        const color = own ? current ? theme.accent : theme.ink
          : trade.direction === 'BUY' ? up : trade.direction === 'SELL' ? down : theme.ink
        if (own && replayTradeKey(trade) === selectedTrade) {
          ctx.strokeStyle = theme.accent; ctx.beginPath(); ctx.arc(replayTimeX(trade.time_us, visibleRange, layout), replayPriceY(trade.price, view, layout), 10, 0, Math.PI * 2); ctx.stroke()
        }
        drawTrade(ctx, trade, own ? current ? 'current' : 'context' : false, replayTimeX(trade.time_us, visibleRange, layout), replayPriceY(trade.price, view, layout), color)
      }
    }
    const cursorX = replayTimeX(projection.time_us, visibleRange, layout)
    if (mode === 'review' && cursorInside) {
      ctx.fillStyle = theme.accent; ctx.globalAlpha = .045; ctx.fillRect(cursorX, layout.top, layout.right - cursorX, layout.bottom - layout.top); ctx.globalAlpha = 1
      if (layout.right - cursorX > 160) { ctx.fillStyle = theme.ink; ctx.fillText('游标之后 · 已录数据', cursorX + 10, layout.top + 18) }
    }
    ctx.restore()
    if (cursorInside) canvasLine(ctx, cursorX, layout.top, cursorX, height - 18, theme.accent)
    const tickStep = Math.max(1, Math.ceil((view.max - view.min) / view.tick / Math.max(2, Math.floor((layout.bottom - layout.top) / 24)))) * view.tick
    for (let price = Math.ceil(view.min / tickStep) * tickStep; price <= view.max; price += tickStep) {
      const y = replayPriceY(price, view, layout)
      canvasLine(ctx, layout.left, y, layout.right, y, theme.line)
      ctx.fillStyle = theme.ink; ctx.textAlign = 'right'; ctx.fillText(priceText(price), layout.priceX, y + 4); ctx.textAlign = 'left'
    }
    drawTimeAxis(ctx, height, visibleRange, layout, theme)
  }, [size, height, projection, replayWindow, visibleRange, symbol, executionId, selectedOrder, selectedTrade, selected, view, layout, historyQuotes, historyOrders, marketSegments, currentQuote, ownOrders, priceText, quantityText, heat, maxHeat, recorded, drawThrough, mode, cursorInside])

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
  const timeline = useMemo(() => replayOrderTimeline(historyOrders, symbol, visibleRange, recorded), [historyOrders, symbol, visibleRange, recorded])
  const inspectAt = (x: number, y: number, pinned = false) => {
    if (!view || y < layout.top || y > layout.bottom || x < layout.left) { setHover(null); return }
    const time = x > layout.right ? projection.time_us : replayXTime(x, visibleRange, layout)
    if (time > drawThrough) { setHover(null); return }
    const price = replayYPrice(y, view, layout)
    let order: ReplayOrder | undefined, trade: ReplayTrade | undefined, market = false
    if (x >= layout.ordX - 8 && layout.columns) order = ownOrders.find(item => item.price != null && Math.abs(replayPriceY(item.price, view, layout) - y) <= 10)
    if (x <= layout.right) {
      const nearTrade = (item: ReplayTrade) => item.symbol === symbol && item.time_us >= visibleRange.start_us && item.time_us < visibleRange.end_us && item.time_us <= drawThrough && item.price != null && item.quantity != null && item.quantity > 0 && Math.hypot(replayTimeX(item.time_us, visibleRange, layout) - x, replayPriceY(item.price, view, layout) - y) <= 10
      trade = replayWindow.context.streams?.[symbol]?.own_trade?.availability === 'unavailable' ? undefined : recorded.ownTrades.find(nearTrade)
      if (!trade) { trade = replayWindow.context.streams?.[symbol]?.market_trade?.availability === 'unavailable' ? undefined : recorded.marketTrades.find(nearTrade); market = trade != null }
      if (!trade) {
        order = timeline.marks.find(mark => Math.hypot(replayTimeX(mark.time, visibleRange, layout) - x, replayPriceY(mark.price, view, layout) - y) <= 9)?.order
          ?? timeline.segments.find(segment => time >= segment.start && time <= segment.end && segment.order.price != null && Math.abs(replayPriceY(segment.order.price, view, layout) - y) <= 6)?.order
      }
    }
    const quote = x > layout.right ? currentQuote : currentReplayQuote(historyQuotes.findLast(item => item.time_us <= time), time, replayWindow.gaps)
    const level = quote && [...quote.bids.map(item => ({ ...item, side: '买' })), ...quote.asks.map(item => ({ ...item, side: '卖' }))].find(item => Math.abs(replayPriceY(item.price, view, layout) - y) <= Math.max(3, (layout.bottom - layout.top) * view.tick / (view.max - view.min) / 2))
    const direction = (value: string | null) => value === 'BUY' ? '买' : value === 'SELL' ? '卖' : '方向未知'
    const detailText = trade ? `${market ? '市场成交' : replayOwnership(trade, executionId).label + '成交'} · ${direction(trade.direction)} ${quantityText(trade.quantity)} ${instrument.quantity_label ?? ''} @ ${priceText(trade.price)}\n${replayTime(trade.time_us)}${!market ? ` · 委托 ${trade.order_id ?? '待关联'} · ${replayTradeLiquidity(trade)}` : ''}${trade.conflict || trade.identity_quality !== 'stable' ? '\n成交身份未确认' : ''}`
      : order ? `${replayOwnership(order, executionId).label} · 委托 ${order.order_id}\n${direction(order.direction)} ${quantityText(order.volume)} @ ${priceText(order.price)} · ${order.cancel_pending ? '撤单待确认' : replayStatusLabels[order.status ?? 'unknown'] ?? '状态未知'}\n当时剩余 ${quantityText(order.remaining_volume)} · ${replayTime(order.time_us)}`
      : `${x > layout.right ? '游标盘口' : '历史盘口'} · ${level ? `${level.side} ${priceText(level.price)} · 挂量 ${quantityText(level.quantity)} ${instrument.quantity_label ?? ''}` : `${priceText(Math.round(price / view.tick) * view.tick)} · 挂量未知／未覆盖`}\n${replayTime(time)}`
    const text = `${(trade?.time_us ?? order?.time_us ?? time) > projection.time_us ? '后续已录 · ' : ''}${detailText}`
    const next = { cursor: hoverCursor, text, x, y, time: trade?.time_us ?? time, order: order?.key, trade: trade && !market ? replayTradeKey(trade) : undefined, pinned }
    setHover(next)
    if (pinned) { onInspect(); if (next.trade) onSelectTrade(next.trade); else if (next.order) onSelectOrder(next.order) }
    return next
  }
  const pointerPosition = (event: PointerEvent<HTMLCanvasElement>) => { const bounds = event.currentTarget.getBoundingClientRect(); return { x: event.clientX - bounds.left, y: event.clientY - bounds.top } }
  const pointerMove = (event: PointerEvent<HTMLCanvasElement>) => {
    const { x, y } = pointerPosition(event), drag = gesture.current
    if (!drag) { if (!activeHover?.pinned && hoverCloseTimer.current == null) inspectAt(x, y); return }
    const dx = x - drag.x, dy = y - drag.y
    if (!drag.moved && Math.hypot(dx, dy) < 3) return
    drag.moved = true; setHover(null)
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
      if (x < 0 || x > layout.right || y < layout.top) return
      event.preventDefault(); setHover(null)
      const delta = (event.deltaY || event.deltaX) * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? size.height : 1)
      const factor = Math.exp(Math.max(-1, Math.min(1, delta / 400)))
      if (x >= layout.left && x <= layout.right) setTimeView(zoomReplayTime(visibleRange, factor, replayXTime(x, visibleRange, layout), range))
      if (y <= layout.bottom) setManualView(zoomReplayPriceAt(view, factor, replayYPrice(y, view, layout)))
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [view, visibleRange, range, layout, size.height])
  const outside = view ? ownOrders.filter(order => order.price != null && (order.price < view.min || order.price > view.max)) : []
  const bookRows: { price: number; quantity: number | null; side: string }[] = currentQuote ? [...currentQuote.asks.map(level => ({ ...level, side: '卖' })), ...currentQuote.bids.map(level => ({ ...level, side: '买' }))] : []
  for (const order of ownOrders) if (order.price != null && !bookRows.some(level => level.price === order.price)) bookRows.push({ price: order.price, quantity: null, side: '档外' })
  bookRows.sort((a, b) => b.price - a.price)
  const outsideDepth = currentQuote && view ? recordedDepth(currentQuote).filter(level => level.price < view.min || level.price > view.max).length : 0
  const focusOrder = () => {
    if (!selected || !view) return
    onInspect(); setTimeView(replayOrderFocus(selected, scene, range)); setHover(null)
    if (selected.price != null && (selected.price < view.min || selected.price > view.max)) setManualView({ ...view, min: Math.min(view.min, selected.price - view.tick * 2), max: Math.max(view.max, selected.price + view.tick * 2) })
  }

  return <section aria-label="历史深度与自有委托" className="replay-chart" data-review-mode={mode}>
    <div className="replay-chart-playback">{symbolControl}{children}<Segmented value={mode} options={[{ value: 'review', label: overviewLabel }, { value: 'step', label: '逐步回放' }]} onChange={value => withViewTransition(() => { onInspect(); setMode(value); setHover(null) })} size="sm" />
      <div className="replay-chart-view-controls" role="group" aria-label="共享价位范围">
        <Tooltip content="聚焦委托过程"><button type="button" className={controlClass} aria-label="聚焦委托过程" disabled={!view || !selected} onClick={focusOrder}><Crosshair size={14} aria-hidden /></button></Tooltip>
        <button type="button" className={controlClass} popoverTarget="replay-chart-legend" aria-label="图层与图例说明"><Info size={15} aria-hidden /></button>
        <button className={controlClass} onClick={() => zoom(0.7)} disabled={!view} aria-label="放大图表"><Plus size={15} aria-hidden /></button>
        <button className={controlClass} onClick={() => zoom(1.4)} disabled={!view} aria-label="缩小图表"><Minus size={15} aria-hidden /></button>
        <button className={controlClass} onClick={resetView} disabled={!manualView && !timeView} aria-label="恢复图表视图"><RotateCcw size={15} aria-hidden /></button>
      </div>
    </div>
    <div className="replay-chart-meta"><div className="replay-chart-legend"><span className="replay-depth-bid"><Minus size={14} aria-hidden />买一</span><span className="replay-depth-ask"><Minus size={14} aria-hidden />卖一</span><span><Minus size={14} aria-hidden />成交价</span><span><Circle size={10} aria-hidden />市场成交</span><span className="text-accent"><Minus size={14} aria-hidden />自有委托</span><span className="text-accent"><Diamond size={10} aria-hidden />本次成交</span></div><span className="replay-heat-scale"><i aria-hidden />{heat?.cells.some(cell => cell.quantity != null) ? `挂量 0 → ${quantityText(maxHeat)}` : '挂量尚未知'}</span>{!cursorInside && <button type="button" className="text-accent" onClick={() => { const span = visibleRange.end_us - visibleRange.start_us; setTimeView(clampReplayView({ start_us: projection.time_us - span / 2, end_us: projection.time_us + span / 2 }, range)) }}>游标在{projection.time_us < visibleRange.start_us ? '左' : '右'}侧 · 回到游标</button>}</div>
    <div ref={holder} className="replay-chart-plot">
      <p id="replay-scene-summary" className="sr-only">{mode === 'review' ? overviewLabel : '逐步回放'}，已录自有成交 {recorded.ownTrades.filter(trade => trade.symbol === symbol && trade.time_us >= visibleRange.start_us && trade.time_us < visibleRange.end_us).length} 笔。盘口与委托状态对应游标 {replayTime(projection.time_us)}。</p>
      <canvas ref={canvas} style={{ height }} className="block w-full touch-none cursor-crosshair focus-visible:outline-2 focus-visible:outline-accent" tabIndex={0}
        aria-describedby="replay-scene-summary"
        aria-label="历史盘口热力图与共享价格轴；拖动浏览，滚轮缩放，方向键平移，正负键缩放，Home 复位" role="group"
        onPointerDown={event => { if (!view || event.button !== 0) return; const { x, y } = pointerPosition(event); if (y < layout.top) return; if (x > layout.right) return; event.currentTarget.focus(); event.currentTarget.setPointerCapture(event.pointerId); gesture.current = { id: event.pointerId, x, y, view, range: visibleRange, mode: x < layout.left ? 'price' : cursorInside && Math.abs(x - replayTimeX(projection.time_us, visibleRange, layout)) <= 8 ? 'seek' : y > layout.bottom ? 'time' : 'chart', moved: false } }}
        onPointerMove={pointerMove} onPointerLeave={() => { if (!gesture.current) leaveHover() }}
        onPointerUp={event => { const drag = gesture.current; gesture.current = null; if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); if (drag && !drag.moved) { const { x, y } = pointerPosition(event); if (x >= layout.left && x <= layout.right) { const hit = inspectAt(x, y, true); onSeek(hit?.trade ? hit.time : replayXTime(x, visibleRange, layout)); setHover(null) } } }}
        onPointerCancel={() => { gesture.current = null; setHover(null) }} onLostPointerCapture={() => { gesture.current = null }}
        onKeyDown={event => {
          if (event.key.startsWith('Arrow')) { event.preventDefault(); pan(event.key === 'ArrowLeft' ? -0.15 : event.key === 'ArrowRight' ? 0.15 : 0, event.key === 'ArrowUp' ? 0.15 : event.key === 'ArrowDown' ? -0.15 : 0) }
          else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(0.7) }
          else if (event.key === '-') { event.preventDefault(); zoom(1.4) }
          else if (event.key === 'Home') { event.preventDefault(); resetView() }
          else if (event.key === 'Enter' && view) { event.preventDefault(); inspectAt(replayTimeX(projection.time_us, visibleRange, layout), replayPriceY(selected?.price ?? (view.min + view.max) / 2, view, layout), true) }
          else if (event.key === 'Escape') setHover(null)
        }} />
      <div className="replay-chart-axis" data-outside={!cursorInside || undefined} style={{ left: layout.left - 12, right: size.width - layout.right - 12, top: height - 30 }}>
        <ValueSlider variant="axis" label="录制定位" min={0} max={Math.max(1, visibleRange.end_us - visibleRange.start_us - 1)} step={1} value={projection.time_us - visibleRange.start_us}
          formatValue={offset => cursorInside ? replayTime(visibleRange.start_us + offset) : `游标在当前视野之外，${replayTime(projection.time_us)}`} onChange={offset => { setHover(null); onSeek(visibleRange.start_us + offset) }} />
      </div>
      {view && layout.columns && <ReplayDepthLadder quote={currentQuote} orders={ownOrders} view={view} layout={layout} height={height} executionId={executionId} selected={selectedOrder} onSelect={key => { onInspect(); onSelectOrder(key) }} onZoom={setManualView} priceText={priceText} quantityText={quantityText} />}
      {activeHover && <>
        {activeHover.x <= layout.right && <div className="replay-crosshair" aria-hidden style={{ left: activeHover.x, top: layout.top, height: layout.bottom - layout.top }} />}
        <div className="replay-crosshair-horizontal" aria-hidden style={{ left: layout.left, right: 6, top: activeHover.y }} />
        <div role="group" aria-label="图中详情" className={`replay-chart-tooltip num ${activeHover.pinned ? 'is-pinned' : ''}`} onPointerEnter={keepHover} onPointerLeave={leaveHover} onFocusCapture={keepHover} onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) leaveHover() }} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); keepHover(); setHover(null); canvas.current?.focus() } }} style={{ left: Math.max(0, Math.min(activeHover.x - 8, size.width - 324)), top: Math.max(0, Math.min(activeHover.y - 8, size.height - 164)) }}>
          <div className="replay-chart-tooltip-card">
          <p>{activeHover.text}</p>
          <div className="replay-hover-actions"><button type="button" className={controlClass} aria-label={activeHover.pinned ? '取消固定详情' : '固定详情'} aria-pressed={Boolean(activeHover.pinned)} onClick={() => { keepHover(); onInspect(); setHover({ ...activeHover, pinned: !activeHover.pinned }) }}><Pin size={13} aria-hidden />{activeHover.pinned ? '已固定' : '固定'}</button>{(activeHover.order || activeHover.trade) && <button type="button" className={controlClass} onClick={() => { onInspect(); if (activeHover.trade) onSelectTrade(activeHover.trade); else if (activeHover.order) onSelectOrder(activeHover.order) }}>查看{activeHover.trade ? '成交' : '委托'}</button>}<button type="button" className={controlClass} onClick={() => { keepHover(); onSeek(activeHover.time); setHover(null) }}>定位此时刻</button><button type="button" className={controlClass} onClick={() => { keepHover(); setHover(null); canvas.current?.focus() }} aria-label="关闭图中详情"><X size={13} aria-hidden /></button></div>
          </div>
        </div>
      </>}
    </div>
    {outsideDepth > 0 && <button className="replay-depth-outside text-accent" type="button" onClick={() => setManualView(null)}>视野外还有 {outsideDepth} 个已录档位 · 显示全部</button>}
    <div className="replay-outside-orders flex flex-wrap gap-2" aria-label="价位范围之外的自有委托">{outside.map(order => <button key={order.key} className="min-h-8 cursor-pointer rounded-chip bg-fill px-2 text-xs text-ink-2 focus-visible:outline-2 focus-visible:outline-accent" onClick={() => { onSelectOrder(order.key); if (view && order.price != null) setManualView(centerReplayPrice(view, order.price)) }}>{order.price! > view!.max ? '上方' : '下方'} {order.order_id} · {replayOwnership(order, executionId).label} · {priceText(order.price)} · 定位</button>)}</div>
    <div className={'sr-only'}><table className="num w-full text-right text-xs"><caption className="mb-2 text-left text-ink-2">当前游标 · 价格 / COB / ORD{instrument?.quantity_label ? `（${instrument.quantity_label}）` : ''}</caption><thead><tr className="text-ink-2"><th className="py-2 text-left">方向</th><th>价格</th><th>市场量</th><th>账户挂单</th></tr></thead><tbody>{bookRows.map((level, index) => {
      const own = ownOrders.filter(order => order.price === level.price)
      return <tr key={`${level.side}:${level.price}:${index}`} className="border-t border-line"><th className="py-2 text-left font-normal">{level.side}</th><td>{priceText(level.price)}</td><td>{quantityText(level.quantity)}</td><td>{own.length ? replayOrderOwnershipGroups(own, executionId).map(group => <span key={group.kind} className="block">{group.label} {quantityText(replayRemainingQuantity(group.orders))} · {group.orders.length}单</span>) : '—'}</td></tr>
    })}</tbody></table>{!currentQuote && <p className="py-3 text-xs text-ink-2">盘口未知／已失效；自有委托仍可在订单表查看。</p>}</div>
    <div id="replay-chart-legend" popover="auto" className="replay-chart-help" aria-label="图层与图例说明">
      <div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-medium">图层与图例</h2><button type="button" popoverTarget="replay-chart-legend" popoverTargetAction="hide" className={controlClass} aria-label="关闭图例"><X size={15} aria-hidden /></button></div>
      <p>显示每帧实际录到的全部买卖档位，档数可随时间变化。热力图由浅至深表示挂量从少到多，竖纹表示量未知；零量只留淡痕。有限盘口不代表完整深度。</p><p className="mt-2">买一、卖一是盘口两侧额外强调的边界，白线只连接已录制的市场成交。三者共用线性价格轴；缺口或过期时盘口边界断开。</p><p className="mt-2">右侧圆点与引导线标明真实价格位置；挂量柱与价格、数量在同一读数行，行间距仅为阅读避让，不是价格距离。档位太多时按价格区间明确聚合，显示区间峰值与档数，点击可放大。热力图每个像素价区间也取峰值，均不把挂量相加。</p><p className="mt-2">委托轨迹：圆圈为挂单起点，方框为改价，三角为已成交，叉号为撤销、过期或拒绝；实线为本次，点线为账户上下文；长虚线为待撤。终态后不延长挂单线，未观察到的挂单过程不补画。</p>
      <p className="mt-2"><Diamond size={12} className="inline" aria-hidden /> 本次成交 · <Square size={12} className="inline" aria-hidden /> 账户上下文成交 · <Circle size={12} className="inline" aria-hidden /> 市场成交</p>
      <p className="mt-2">选中委托时，黄色空框为已经关联的报单请求；请求不代表已挂入盘口，后续圆圈才是首次场内确认。点击“委托过程”展开这段已录时间，不改变回放游标。</p>
      <p className="mt-2">拖动主图平移；滚轮以鼠标位置缩放；拖动价格轴可单独缩放价位。拖动浏览不改变游标；点击主图、拖动图内时间游标或点击“定位此时刻”才定位。整段复盘展示已读取时段的全部记录，逐步回放只展示游标之前；右侧盘口与底部状态始终对应游标。</p>
      <p className="mt-2">账户挂单是游标时刻该账户在此价位有效委托的未成交剩余量，包含其他执行；不代表持仓或排队位置，缺少剩余量时保持未知。悬停盘口行可看本次数量。</p>
      <p className="mt-2">悬停详情可移入操作或固定；查看与固定不改变游标，点击定位才恢复对应时刻。画布获得焦点后按 Enter 可固定当前价位详情，Escape 关闭。</p>
      <div className="mt-3 max-h-64 overflow-auto"><table className="num w-full text-right text-xs"><caption className="mb-2 text-left">当前游标盘口</caption><thead><tr><th>方向</th><th>价格</th><th>市场量</th></tr></thead><tbody>{bookRows.map((level, index) => <tr key={`${level.side}:${level.price}:${index}`} className="border-t border-line"><td className="py-1">{level.side}</td><td>{priceText(level.price)}</td><td>{quantityText(level.quantity)}</td></tr>)}</tbody></table></div>
    </div>
  </section>
}
