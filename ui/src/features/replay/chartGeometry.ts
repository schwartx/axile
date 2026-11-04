import type { ReplayGap, ReplayOrder, ReplayQuote, ReplayRange, ReplayTrade } from '@/features/replay/types'

export interface ReplayChartLayout { left: number; right: number; top: number; bottom: number; priceX: number; cobX: number; ordX: number; columns: boolean }
export interface ReplayPriceView { min: number; max: number; tick: number }

/** 场内身份与剩余量完整性分开；未知量仍保留委托，不冒充零量。 */
export function replayOnBookOrder(order: ReplayOrder): boolean {
  return order.confirmed && !order.terminal && order.price != null && (order.remaining_volume == null || order.remaining_volume > 0)
}

export function replayRemainingQuantity(orders: Pick<ReplayOrder, 'remaining_volume'>[]): number | null {
  return orders.some(order => order.remaining_volume == null) ? null : orders.reduce((sum, order) => sum + order.remaining_volume!, 0)
}

export function replayChartLayout(width: number, height: number, priceAxisWidth = 40): ReplayChartLayout {
  const columns = width >= 600
  const right = width - (columns ? 346 : 12) - priceAxisWidth
  return { left: 12, right, top: 0, bottom: height - 44,
    priceX: right + priceAxisWidth - 8, cobX: width - 334, ordX: width - 68, columns }
}

/** 坐标刻度使用规整间隔，按序号计算以避免反复累加浮点价差。 */
export function replayPriceTicks(view: ReplayPriceView, height: number): number[] {
  const spacing = Math.max(view.tick, (view.max - view.min) / Math.max(2, Math.floor(height / 24)))
  const magnitude = 10 ** Math.floor(Math.log10(spacing))
  const step = [1, 2, 5, 10].find(value => value * magnitude >= spacing)! * magnitude
  const first = Math.ceil(view.min / step), last = Math.floor(view.max / step)
  return Array.from({ length: Math.max(0, last - first + 1) }, (_, index) => Number(((first + index) * step).toPrecision(15)))
}

export function replayPriceY(price: number, view: ReplayPriceView, layout: ReplayChartLayout): number {
  return layout.top + (view.max - price) / (view.max - view.min) * (layout.bottom - layout.top)
}

export function replayYPrice(y: number, view: ReplayPriceView, layout: ReplayChartLayout): number {
  return view.max - (y - layout.top) / (layout.bottom - layout.top) * (view.max - view.min)
}

export function replayTimeX(timeUs: number, range: ReplayRange, layout: ReplayChartLayout): number {
  return layout.left + (timeUs - range.start_us) / (range.end_us - range.start_us) * (layout.right - layout.left)
}

export function replayXTime(x: number, range: ReplayRange, layout: ReplayChartLayout): number {
  const ratio = Math.max(0, Math.min(1, (x - layout.left) / (layout.right - layout.left)))
  return Math.min(range.end_us - 1, Math.round(range.start_us + ratio * (range.end_us - range.start_us)))
}

export function depthGap(gaps: ReplayGap[], timeUs: number, symbol: string): boolean {
  return gaps.some(gap => timeUs >= gap.start_us && timeUs < gap.end_us && (!gap.symbol || gap.symbol === symbol)
    && (!gap.topic || gap.topic === 'depth' || gap.topic.startsWith('/quote')))
}

export function currentReplayQuote(quote: ReplayQuote | undefined, timeUs: number, gaps: ReplayGap[]): ReplayQuote | null {
  if ((!quote?.book_valid || !quote.available) || quote.time_us > timeUs || depthGap(gaps, timeUs, quote.symbol)) return null
  return quote.time_us === timeUs || (quote.valid_until_us != null && timeUs < quote.valid_until_us && replayQuoteEnd(quote, timeUs, timeUs, gaps) === timeUs) ? quote : null
}

/** 未声明有效区间的快照只是一处观测，不延展为连续深度。 */
export function replayQuoteEnd(quote: ReplayQuote, nextTimeUs: number, cursorUs: number, gaps: ReplayGap[]): number {
  if ((!quote.book_valid || !quote.available) || depthGap(gaps, quote.time_us, quote.symbol)) return quote.time_us
  let end = Math.min(nextTimeUs, cursorUs, quote.valid_until_us ?? quote.time_us)
  for (const gap of gaps) {
    if ((!gap.symbol || gap.symbol === quote.symbol) && (!gap.topic || gap.topic === 'depth' || gap.topic.startsWith('/quote'))
      && gap.start_us >= quote.time_us && gap.start_us < end) end = gap.start_us
  }
  return Math.max(quote.time_us, end)
}

export function defaultReplayPriceView(quote: Pick<ReplayQuote, 'bids' | 'asks'> | undefined, orders: ReplayOrder[], tick?: number | null, marketTrades: ReplayTrade[] = []): ReplayPriceView | null {
  const prices = quote ? [...quote.bids, ...quote.asks].map(level => level.price).filter(price => Number.isFinite(price) && price > 0) : []
  if (!prices.length) prices.push(...orders.flatMap(order => order.price != null && order.price > 0 ? [order.price] : []))
  if (!prices.length) prices.push(...marketTrades.flatMap(trade => trade.price != null && Number.isFinite(trade.price) && trade.price > 0 ? [trade.price] : []))
  if (!prices.length) return null
  prices.sort((a, b) => a - b)
  const differences = prices.slice(1).map((price, index) => price - prices[index]).filter(value => value > 0)
  const step = tick != null && Number.isFinite(tick) && tick > 0 ? tick : differences.length ? Math.min(...differences) : Math.max(Math.abs(prices[0]) * 0.0001, 0.000001)
  return { min: prices[0] - step * 2, max: prices[prices.length - 1] + step * 2, tick: step }
}

export function zoomReplayPrice(view: ReplayPriceView, factor: number): ReplayPriceView {
  const center = (view.min + view.max) / 2
  const half = Math.max(view.tick * 2, (view.max - view.min) * factor / 2)
  return { ...view, min: center - half, max: center + half }
}

export function centerReplayPrice(view: ReplayPriceView, price: number): ReplayPriceView {
  const half = (view.max - view.min) / 2
  return { ...view, min: price - half, max: price + half }
}

/** 只连接已录制的市场成交价，窗口外、未来和缺口都不延线。 */
export function replayMarketPriceSegments(trades: ReplayTrade[], symbol: string, range: ReplayRange, cursorUs: number, gaps: ReplayGap[]): ReplayTrade[][] {
  const marketGaps = gaps.filter(gap => {
    const topic = gap.topic?.replace(/^\/+/, '')
    return (!gap.symbol || gap.symbol === symbol) && (!topic || topic === 'market_trade' || topic.startsWith('market_trade/'))
  })
  const visible = trades.filter(trade => trade.symbol === symbol && Number.isFinite(trade.time_us)
    && trade.time_us >= range.start_us && trade.time_us < range.end_us && trade.time_us <= cursorUs)
    .sort((left, right) => left.time_us - right.time_us || left.capture_seq - right.capture_seq)
  const segments: ReplayTrade[][] = []
  let previous: ReplayTrade | null = null
  for (const trade of visible) {
    if (trade.conflict || trade.price == null || !Number.isFinite(trade.price) || trade.price <= 0) { previous = null; continue }
    if (marketGaps.some(gap => trade.time_us >= gap.start_us && trade.time_us < gap.end_us)) { previous = null; continue }
    if (!previous || marketGaps.some(gap => previous!.time_us < gap.end_us && trade.time_us >= gap.start_us)) segments.push([])
    segments[segments.length - 1].push(trade)
    previous = trade
  }
  return segments
}

/** 同一屏幕格取峰值，显示压缩不能把瞬时大挂量平均掉。 */
export function replayHeatCells(quotes: ReplayQuote[], range: ReplayRange, cursorUs: number, view: ReplayPriceView, layout: ReplayChartLayout, gaps: ReplayGap[]) {
  const cells = new Map<string, { x: number; y: number; quantity: number | null; unknown: boolean; minPrice: number; maxPrice: number }>()
  const columnWidth = 3, rowHeight = Math.max(2, Math.min(18, (layout.bottom - layout.top) * view.tick / (view.max - view.min)))
  for (let index = 0; index < quotes.length; index++) {
    const quote = quotes[index]
    if (quote.time_us > cursorUs || (!quote.book_valid || !quote.available) || depthGap(gaps, quote.time_us, quote.symbol)) continue
    const end = replayQuoteEnd(quote, quotes[index + 1]?.time_us ?? cursorUs, cursorUs, gaps)
    const fromX = Math.max(layout.left, replayTimeX(Math.max(range.start_us, quote.time_us), range, layout))
    const endX = Math.min(layout.right, replayTimeX(end, range, layout))
    if (end < range.start_us || fromX > layout.right) continue
    for (const level of [...quote.bids, ...quote.asks]) {
      if (!Number.isFinite(level.price) || !(level.price > 0) || level.price < view.min || level.price > view.max) continue
      const rawY = replayPriceY(level.price, view, layout)
      const y = rowHeight === 2 ? Math.floor(rawY / 2) * 2 + 1 : rawY
      for (let x = Math.floor(fromX / columnWidth) * columnWidth; x <= Math.max(fromX, endX - 1); x += columnWidth) {
        const key = `${x}:${y}`, previous = cells.get(key)
        cells.set(key, { x, y, quantity: level.quantity == null ? previous?.quantity ?? null : Math.max(previous?.quantity ?? 0, level.quantity),
          unknown: level.quantity == null || previous?.unknown === true,
          minPrice: Math.min(previous?.minPrice ?? level.price, level.price), maxPrice: Math.max(previous?.maxPrice ?? level.price, level.price) })
      }
    }
  }
  return { cells: [...cells.values()], columnWidth, rowHeight }
}
