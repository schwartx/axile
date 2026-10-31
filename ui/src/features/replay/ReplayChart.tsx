import { useCallback, useLayoutEffect, useMemo, useRef, useState, type MouseEvent, type PointerEvent } from 'react'
import { ArrowDown, ArrowUp, Circle, Diamond, Minus, Plus, RotateCcw } from 'lucide-react'
import { canvasLine, prepareCanvas, readCanvasTheme } from '@/components/viz/chartCanvas'
import { currentReplayQuote, defaultReplayPriceView, replayChartLayout, replayHeatCells, replayOnBookOrder, replayPriceY, replayRemainingQuantity, replayTimeX, replayXTime, replayYPrice, zoomReplayPrice, type ReplayChartLayout, type ReplayPriceView } from '@/features/replay/chartGeometry'
import { replayInstrument, replayNumber, replayTime } from '@/features/replay/presentation'
import type { ReplayOrder, ReplayProjection, ReplayRange, ReplayTrade, ReplayWindow } from '@/features/replay/types'

interface Props {
  window: ReplayWindow
  range: ReplayRange
  projection: ReplayProjection
  symbol: string
  selectedOrder: string | null
  onSelectOrder: (key: string) => void
  onSeek: (timeUs: number) => void
  onStep?: (direction: -1 | 1) => void
}

const controlClass = 'inline-flex min-h-9 min-w-9 cursor-pointer items-center justify-center rounded-chip text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-40'

function drawTrade(ctx: CanvasRenderingContext2D, trade: ReplayTrade, own: boolean, x: number, y: number, color: string) {
  const radius = Math.min(11, Math.max(3, Math.sqrt(trade.quantity ?? 0) * 1.8))
  ctx.fillStyle = ctx.strokeStyle = color
  ctx.globalAlpha = own ? 1 : 0.62
  ctx.beginPath()
  if (own) { ctx.moveTo(x, y - radius); ctx.lineTo(x + radius, y); ctx.lineTo(x, y + radius); ctx.lineTo(x - radius, y); ctx.closePath() }
  else ctx.arc(x, y, radius, 0, Math.PI * 2)
  if (trade.identity_quality === 'stable') ctx.fill()
  else ctx.stroke()
  ctx.globalAlpha = 1
}

function drawOrderHistory(ctx: CanvasRenderingContext2D, history: ReplayOrder[], projection: ReplayProjection, range: ReplayRange, view: ReplayPriceView, layout: ReplayChartLayout, selected: string | null, accent: string, warn: string) {
  const groups = new Map<string, ReplayOrder[]>()
  for (const order of history) { const rows = groups.get(order.key) ?? []; rows.push(order); groups.set(order.key, rows) }
  for (const [key, orders] of groups) {
    for (let index = 0; index < orders.length; index++) {
      const order = orders[index]
      if (!replayOnBookOrder(order) || order.price == null || order.price < view.min || order.price > view.max) continue
      const from = Math.max(range.start_us, order.time_us)
      const until = Math.min(projection.time_us, orders[index + 1]?.time_us ?? projection.time_us)
      if (until < from) continue
      const startX = Math.max(layout.left, replayTimeX(from, range, layout)), endX = Math.min(layout.right, replayTimeX(until, range, layout))
      const y = replayPriceY(order.price, view, layout)
      ctx.strokeStyle = order.cancel_pending ? warn : accent
      ctx.lineWidth = key === selected ? 3 : 1.7
      ctx.setLineDash(order.cancel_pending ? [5, 3] : [])
      ctx.beginPath(); ctx.moveTo(startX, y); ctx.lineTo(Math.max(startX + 1, endX), y); ctx.stroke()
      ctx.setLineDash([])
      if (index === 0 && !order.carry_in && order.time_us >= range.start_us) { ctx.beginPath(); ctx.arc(startX, y, 2.8, 0, Math.PI * 2); ctx.stroke() }
      if (order.carry_in && from === range.start_us) { ctx.beginPath(); ctx.moveTo(startX + 5, y - 3); ctx.lineTo(startX, y); ctx.lineTo(startX + 5, y + 3); ctx.stroke() }
      const next = orders[index + 1]
      if (next?.confirmed && !next.terminal && next.price != null && next.price !== order.price && next.time_us <= projection.time_us) {
        ctx.beginPath(); ctx.moveTo(endX, y); ctx.lineTo(endX, replayPriceY(next.price, view, layout)); ctx.stroke()
      }
    }
  }
  ctx.lineWidth = 1
}

/** 同一画布中的历史深度、COB 与 ORD 只共享一份价位映射。 */
export function ReplayChart({ window: replayWindow, range, projection, symbol, selectedOrder, onSelectOrder, onSeek, onStep }: Props) {
  const holder = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null)
  const [size, setSize] = useState({ width: 900, revision: 0 })
  const [manualView, setManualView] = useState<ReplayPriceView | null>(null)
  const [hover, setHover] = useState<{ cursor: string; text: string } | null>(null)
  const hoverCursor = `${replayWindow.window_id}:${symbol}:${projection.time_us}:${projection.capture_seq}`
  const instrument = replayInstrument(replayWindow.context, symbol)
  const orders = useMemo(() => Object.values(projection.orders).filter(order => order.symbol === symbol), [projection.orders, symbol])
  const currentQuote = useMemo(() => currentReplayQuote(projection.quotes[symbol], projection.time_us, replayWindow.gaps), [projection.quotes, symbol, projection.time_us, replayWindow.gaps])
  const historyQuotes = useMemo(() => projection.quoteHistory.filter(quote => quote.symbol === symbol && quote.time_us <= projection.time_us), [projection.quoteHistory, symbol, projection.time_us])
  const historyOrders = useMemo(() => projection.orderHistory.filter(order => order.symbol === symbol && order.time_us <= projection.time_us), [projection.orderHistory, symbol, projection.time_us])
  const initialView = useMemo(() => defaultReplayPriceView(historyQuotes[0] ?? projection.quotes[symbol], orders, instrument?.price_tick), [historyQuotes, projection.quotes, symbol, orders, instrument?.price_tick])
  const view = manualView ?? initialView
  const height = size.width < 720 ? 350 : 440
  const layout = useMemo(() => replayChartLayout(size.width, height), [size.width, height])
  const ownOrders = useMemo(() => orders.filter(replayOnBookOrder), [orders])
  const decimals = instrument?.price_decimals ?? 6
  const priceText = useCallback((price: number | null) => replayNumber(price, decimals), [decimals])
  const quantityText = useCallback((quantity: number | null) => replayNumber(quantity, instrument?.quantity_decimals ?? 6), [instrument?.quantity_decimals])

  useLayoutEffect(() => { setManualView(null) }, [replayWindow.window_id, symbol])
  useLayoutEffect(() => {
    if (!holder.current) return
    const element = holder.current
    const measure = () => setSize(previous => ({ width: Math.max(240, element.clientWidth), revision: previous.revision + 1 }))
    measure()
    const resize = new ResizeObserver(measure); resize.observe(element)
    const theme = new MutationObserver(measure); theme.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style'] })
    document.fonts.addEventListener('loadingdone', measure)
    return () => { resize.disconnect(); theme.disconnect(); document.fonts.removeEventListener('loadingdone', measure) }
  }, [])

  useLayoutEffect(() => {
    if (!canvas.current || !holder.current) return
    const ctx = prepareCanvas(canvas.current, size.width, height)
    if (!ctx) return
    const theme = readCanvasTheme(holder.current), css = getComputedStyle(holder.current)
    const up = css.getPropertyValue('--color-up').trim(), down = css.getPropertyValue('--color-down').trim()
    ctx.font = `12px ${theme.font}`; ctx.fillStyle = theme.surface; ctx.fillRect(0, 0, size.width, height)
    ctx.fillStyle = theme.ink; ctx.fillText('历史挂量与自有委托', layout.left, 18)
    if (layout.columns) { ctx.textAlign = 'center'; ctx.fillText('价格', layout.priceX, 18); ctx.fillText('COB · 市场量', layout.cobX + 40, 18); ctx.fillText('ORD · 自有量', layout.ordX + 36, 18); ctx.textAlign = 'left' }
    if (!view) { ctx.fillText('当前游标尚无有效价位；可继续步进查看。', layout.left, height / 2); return }
    const heat = replayHeatCells(historyQuotes, range, projection.time_us, view, layout, replayWindow.gaps)
    const maxQuantity = heat.cells.reduce((maximum, cell) => Math.max(maximum, cell.quantity), 1)
    ctx.save(); ctx.beginPath(); ctx.rect(layout.left, layout.top, layout.right - layout.left, layout.bottom - layout.top); ctx.clip()
    ctx.save(); ctx.beginPath(); ctx.rect(layout.left, layout.top, Math.max(0, replayTimeX(projection.time_us, range, layout) - layout.left), layout.bottom - layout.top); ctx.clip()
    ctx.fillStyle = theme.accent
    for (const cell of heat.cells) { ctx.globalAlpha = cell.quantity === 0 ? 0.025 : 0.06 + Math.sqrt(cell.quantity / maxQuantity) * 0.57; ctx.fillRect(cell.x, cell.y - heat.rowHeight / 2, heat.columnWidth, heat.rowHeight) }
    ctx.globalAlpha = 1
    ctx.restore()
    for (const gap of replayWindow.gaps) {
      if ((gap.symbol && gap.symbol !== symbol) || gap.start_us > projection.time_us || (gap.topic && gap.topic !== 'depth' && !gap.topic.startsWith('/quote'))) continue
      const x = replayTimeX(Math.max(range.start_us, gap.start_us), range, layout), end = replayTimeX(Math.min(projection.time_us, gap.end_us), range, layout)
      ctx.fillStyle = theme.fill; ctx.fillRect(x, layout.top, Math.max(1, end - x), layout.bottom - layout.top)
    }
    drawOrderHistory(ctx, historyOrders, projection, range, view, layout, selectedOrder, theme.accent, theme.warn)
    const tradeGroups: [ReplayTrade[], boolean][] = [[projection.marketTrades, false], [projection.ownTrades, true]]
    for (const [trades, own] of tradeGroups) {
      const layer = replayWindow.context.streams?.[symbol]?.[own ? 'own_trade' : 'market_trade']
      if (layer?.availability === 'unavailable') continue
      for (const trade of trades) {
        if (trade.symbol !== symbol || trade.time_us < range.start_us || trade.time_us > projection.time_us || trade.price == null || trade.quantity == null || trade.quantity <= 0) continue
        const color = own ? theme.accent : trade.direction === 'BUY' ? up : trade.direction === 'SELL' ? down : theme.ink
        drawTrade(ctx, trade, own, replayTimeX(trade.time_us, range, layout), replayPriceY(trade.price, view, layout), color)
      }
    }
    const cursorX = replayTimeX(projection.time_us, range, layout)
    ctx.setLineDash([3, 3]); canvasLine(ctx, cursorX, layout.top, cursorX, layout.bottom, theme.ink); ctx.setLineDash([])
    ctx.restore()
    const tickStep = Math.max(1, Math.ceil((view.max - view.min) / view.tick / 14)) * view.tick
    for (let price = Math.ceil(view.min / tickStep) * tickStep; price <= view.max; price += tickStep) {
      const y = replayPriceY(price, view, layout)
      canvasLine(ctx, layout.left, y, layout.columns ? size.width - 6 : layout.right, y, theme.line)
      ctx.fillStyle = theme.ink; ctx.textAlign = 'right'; ctx.fillText(priceText(price), layout.priceX, y + 4); ctx.textAlign = 'left'
    }
    if (layout.columns) {
      canvasLine(ctx, layout.right + 8, layout.top - 9, layout.right + 8, layout.bottom, theme.line)
      const levels = currentQuote ? [...currentQuote.bids.map(level => ({ ...level, side: '买' })), ...currentQuote.asks.map(level => ({ ...level, side: '卖' }))] : []
      const maxBook = Math.max(1, ...levels.map(level => level.quantity))
      for (const level of levels) {
        if (level.price < view.min || level.price > view.max) continue
        const y = replayPriceY(level.price, view, layout)
        ctx.fillStyle = level.side === '买' ? up : down; ctx.globalAlpha = 0.15
        ctx.fillRect(layout.cobX, y - 8, level.quantity / maxBook * 83, 16); ctx.globalAlpha = 1
        ctx.fillStyle = theme.ink; ctx.fillText(`${level.side} ${quantityText(level.quantity)}`, layout.cobX + 4, y + 4)
      }
      if (!currentQuote) { ctx.fillStyle = theme.muted; ctx.fillText('盘口未知／已失效', layout.cobX, layout.top + 16) }
      const ownAtPrice = new Map<number, ReplayOrder[]>()
      for (const order of ownOrders) { if (order.price == null) continue; const rows = ownAtPrice.get(order.price) ?? []; rows.push(order); ownAtPrice.set(order.price, rows) }
      for (const [price, rows] of ownAtPrice) {
        if (price < view.min || price > view.max) continue
        const y = replayPriceY(price, view, layout), pending = rows.some(order => order.cancel_pending)
        ctx.fillStyle = pending ? theme.warn : theme.accent
        if (rows.some(order => order.key === selectedOrder)) { ctx.globalAlpha = 0.1; ctx.fillRect(layout.ordX - 2, y - 9, 90, 18); ctx.globalAlpha = 1 }
        ctx.fillText(`${quantityText(replayRemainingQuantity(rows))} · ${rows.length}单${pending ? ' 待撤' : ''}`, layout.ordX, y + 4)
      }
    }
    ctx.fillStyle = theme.ink
    const labels = Math.max(2, Math.floor((layout.right - layout.left) / 145))
    for (let index = 0; index <= labels; index++) {
      const time = Math.min(range.end_us - 1, Math.round(range.start_us + (range.end_us - range.start_us) * index / labels))
      ctx.textAlign = index === 0 ? 'left' : index === labels ? 'right' : 'center'
      ctx.fillText(replayTime(time, false, false), replayTimeX(time, range, layout), height - 10)
    }
    ctx.textAlign = 'left'
  }, [size, height, projection, replayWindow, range, symbol, selectedOrder, view, layout, historyQuotes, historyOrders, currentQuote, ownOrders, priceText, quantityText])

  const zoom = (factor: number) => { if (view) setManualView(zoomReplayPrice(view, factor)) }
  const pan = (direction: number) => { if (view) { const offset = (view.max - view.min) * 0.3 * direction; setManualView({ ...view, min: view.min + offset, max: view.max + offset }) } }
  const pointer = (event: PointerEvent<HTMLCanvasElement> | MouseEvent<HTMLCanvasElement>, click = false) => {
    if (!view) return
    const bounds = event.currentTarget.getBoundingClientRect(), x = event.clientX - bounds.left, y = event.clientY - bounds.top
    const price = replayYPrice(y, view, layout)
    if (layout.columns && x >= layout.ordX - 8) {
      const candidate = ownOrders.reduce<ReplayOrder | null>((closest, order) => order.price != null && (!closest || Math.abs(replayPriceY(order.price, view, layout) - y) < Math.abs(replayPriceY(closest.price!, view, layout) - y)) ? order : closest, null)
      if (candidate && candidate.price != null && Math.abs(replayPriceY(candidate.price, view, layout) - y) < 12) { setHover({ cursor: hoverCursor, text: `${candidate.order_id} · ${priceText(candidate.price)} · 剩余 ${quantityText(candidate.remaining_volume)}${candidate.cancel_pending ? ' · 撤单待确认' : ''}` }); if (click) onSelectOrder(candidate.key) }
      return
    }
    const time = Math.min(projection.time_us, replayXTime(x, range, layout))
    const quote = [...historyQuotes].reverse().find(item => item.time_us <= time)
    const valid = currentReplayQuote(quote, time, replayWindow.gaps)
    const nearest = valid ? [...valid.bids, ...valid.asks].find(level => Math.abs(level.price - price) <= view.tick / 2) : null
    setHover({ cursor: hoverCursor, text: `${replayTime(time)} · 价位 ${priceText(Math.round(price / view.tick) * view.tick)} · ${nearest ? `市场挂量 ${quantityText(nearest.quantity)} ${instrument?.quantity_label ?? ''}` : '挂量未知／未覆盖'}` })
    if (click && x <= layout.right && x >= layout.left) onSeek(replayXTime(x, range, layout))
  }
  const outside = view ? ownOrders.filter(order => order.price != null && (order.price < view.min || order.price > view.max)) : []
  const bookRows: { price: number; quantity: number | null; side: string }[] = currentQuote ? [...currentQuote.asks.map(level => ({ ...level, side: '卖' })), ...currentQuote.bids.map(level => ({ ...level, side: '买' }))] : []
  for (const order of ownOrders) if (order.price != null && !bookRows.some(level => level.price === order.price)) bookRows.push({ price: order.price, quantity: null, side: '档外' })
  bookRows.sort((a, b) => b.price - a.price)

  return <section aria-label="历史深度与自有委托" className="min-w-0">
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-2">
        <span>市场挂量 <span className="ml-1 inline-flex gap-px" aria-hidden>{[0.1, 0.25, 0.45, 0.65].map(opacity => <i key={opacity} className="h-2.5 w-3 bg-accent" style={{ opacity }} />)}</span> 少 → 多</span>
        <span className="inline-flex items-center gap-1"><Circle size={12} aria-hidden />市场成交</span>
        <span className="inline-flex items-center gap-1 text-accent"><Minus size={15} aria-hidden />自有委托 <Diamond size={12} aria-hidden />自有成交</span>
        <span className="text-warn">虚线：待撤确认</span>
      </div>
      <div className="flex items-center gap-1" role="group" aria-label="共享价位范围">
        <button className={controlClass} onClick={() => pan(1)} disabled={!view} aria-label="价位范围上移"><ArrowUp size={15} aria-hidden /></button>
        <button className={controlClass} onClick={() => pan(-1)} disabled={!view} aria-label="价位范围下移"><ArrowDown size={15} aria-hidden /></button>
        <button className={controlClass} onClick={() => zoom(0.7)} disabled={!view} aria-label="放大价位范围"><Plus size={15} aria-hidden /></button>
        <button className={controlClass} onClick={() => zoom(1.4)} disabled={!view} aria-label="缩小价位范围"><Minus size={15} aria-hidden /></button>
        <button className={controlClass} onClick={() => setManualView(null)} disabled={!manualView} aria-label="恢复价位范围"><RotateCcw size={15} aria-hidden /></button>
      </div>
    </div>
    <div ref={holder} className="overflow-hidden border-y border-line">
      <canvas ref={canvas} style={{ height }} className="block w-full touch-pan-y cursor-crosshair focus-visible:outline-2 focus-visible:outline-accent" tabIndex={0}
        aria-label="历史盘口热力图与共享价格轴；左右键逐事件，正负键缩放价位" role="group" onPointerMove={event => pointer(event)} onClick={event => pointer(event, true)}
        onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); onStep?.(event.key === 'ArrowLeft' ? -1 : 1) } else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(0.7) } else if (event.key === '-') { event.preventDefault(); zoom(1.4) } }} />
    </div>
    <p className="num mt-2 min-h-5 text-xs leading-5 text-ink-2">{hover?.cursor === hoverCursor ? hover.text : '点击历史区域定位时间；选择委托可突出轨迹。'}</p>
    <div className="mt-1 flex flex-wrap gap-2" aria-label="价位范围之外的自有委托">{outside.map(order => <button key={order.key} className="min-h-8 cursor-pointer rounded-chip bg-fill px-2 text-xs text-accent focus-visible:outline-2 focus-visible:outline-accent" onClick={() => { onSelectOrder(order.key); if (view && order.price != null) { const half = (view.max - view.min) / 2; setManualView({ ...view, min: order.price - half, max: order.price + half }) } }}>{order.price! > view!.max ? '上方' : '下方'} {order.order_id} · {priceText(order.price)} · 定位</button>)}</div>
    <div className={size.width < 720 ? 'mt-3 overflow-x-auto' : 'sr-only'}><table className="num w-full text-right text-xs"><caption className="mb-2 text-left text-ink-2">当前游标 · 价格 / COB / ORD{instrument?.quantity_label ? `（${instrument.quantity_label}）` : ''}</caption><thead><tr className="text-ink-2"><th className="py-2 text-left">方向</th><th>价格</th><th>市场量</th><th>自有剩余量</th></tr></thead><tbody>{bookRows.map((level, index) => {
      const own = ownOrders.filter(order => order.price === level.price)
      return <tr key={`${level.side}:${level.price}:${index}`} className="border-t border-line"><th className="py-2 text-left font-normal">{level.side}</th><td>{priceText(level.price)}</td><td>{quantityText(level.quantity)}</td><td>{own.length ? quantityText(replayRemainingQuantity(own)) : '—'}</td></tr>
    })}</tbody></table>{!currentQuote && <p className="py-3 text-xs text-ink-2">盘口未知／已失效；自有委托仍可在订单表查看。</p>}</div>
  </section>
}
