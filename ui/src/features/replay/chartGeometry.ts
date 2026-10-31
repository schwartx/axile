import type { ReplayGap, ReplayOrder, ReplayQuote, ReplayRange } from '@/features/replay/types'

export interface ReplayChartLayout { left: number; right: number; top: number; bottom: number; priceX: number; cobX: number; ordX: number; columns: boolean }
export interface ReplayPriceView { min: number; max: number; tick: number }

/** 场内身份与剩余量完整性分开；未知量仍保留委托，不冒充零量。 */
export function replayOnBookOrder(order: ReplayOrder): boolean {
  return order.confirmed && !order.terminal && order.price != null && (order.remaining_volume == null || order.remaining_volume > 0)
}

export function replayRemainingQuantity(orders: Pick<ReplayOrder, 'remaining_volume'>[]): number | null {
  return orders.some(order => order.remaining_volume == null) ? null : orders.reduce((sum, order) => sum + order.remaining_volume!, 0)
}

export function replayChartLayout(width: number, height: number): ReplayChartLayout {
  const columns = width >= 720
  return { left: columns ? 12 : 68, right: width - (columns ? 278 : 12), top: 34, bottom: height - 32,
    priceX: columns ? width - 233 : 62, cobX: width - 187, ordX: width - 88, columns }
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
  if (!quote?.book_valid || quote.time_us > timeUs || depthGap(gaps, timeUs, quote.symbol)) return null
  return quote.time_us === timeUs || (quote.valid_until_us != null && timeUs < quote.valid_until_us) ? quote : null
}

/** 未声明有效区间的快照只是一处观测，不延展为连续深度。 */
export function replayQuoteEnd(quote: ReplayQuote, nextTimeUs: number, cursorUs: number, gaps: ReplayGap[]): number {
  if (!quote.book_valid || depthGap(gaps, quote.time_us, quote.symbol)) return quote.time_us
  let end = Math.min(nextTimeUs, cursorUs, quote.valid_until_us ?? quote.time_us)
  for (const gap of gaps) {
    if ((!gap.symbol || gap.symbol === quote.symbol) && (!gap.topic || gap.topic === 'depth' || gap.topic.startsWith('/quote'))
      && gap.start_us >= quote.time_us && gap.start_us < end) end = gap.start_us
  }
  return Math.max(quote.time_us, end)
}

export function defaultReplayPriceView(quote: ReplayQuote | undefined, orders: ReplayOrder[], tick?: number | null): ReplayPriceView | null {
  const prices = quote ? [...quote.bids, ...quote.asks].map(level => level.price).filter(price => Number.isFinite(price) && price > 0) : []
  if (!prices.length) prices.push(...orders.flatMap(order => order.price != null && order.price > 0 ? [order.price] : []))
  if (!prices.length) return null
  prices.sort((a, b) => a - b)
  const differences = prices.slice(1).map((price, index) => price - prices[index]).filter(value => value > 0)
  const step = tick != null && Number.isFinite(tick) && tick > 0 ? tick : differences.length ? Math.min(...differences) : Math.max(Math.abs(prices[0]) * 0.0001, 0.000001)
  return { min: prices[0] - step * 2, max: prices[prices.length - 1] + step * 2, tick: step }
}

export function zoomReplayPrice(view: ReplayPriceView, factor: number): ReplayPriceView {
  const center = (view.min + view.max) / 2
  const half = Math.max(view.tick * 2, Math.min((view.max - view.min) * factor / 2, view.tick * 500))
  return { ...view, min: center - half, max: center + half }
}

/** 同一屏幕格取峰值，显示压缩不能把瞬时大挂量平均掉。 */
export function replayHeatCells(quotes: ReplayQuote[], range: ReplayRange, cursorUs: number, view: ReplayPriceView, layout: ReplayChartLayout, gaps: ReplayGap[]) {
  const cells = new Map<string, { x: number; y: number; quantity: number }>()
  const columnWidth = 3, rowHeight = Math.max(2, Math.min(18, (layout.bottom - layout.top) * view.tick / (view.max - view.min)))
  for (let index = 0; index < quotes.length; index++) {
    const quote = quotes[index]
    if (quote.time_us > cursorUs || !quote.book_valid || depthGap(gaps, quote.time_us, quote.symbol)) continue
    const end = replayQuoteEnd(quote, quotes[index + 1]?.time_us ?? cursorUs, cursorUs, gaps)
    const fromX = Math.max(layout.left, replayTimeX(Math.max(range.start_us, quote.time_us), range, layout))
    const endX = Math.min(layout.right, replayTimeX(end, range, layout))
    if (end < range.start_us || fromX > layout.right) continue
    for (const level of [...quote.bids, ...quote.asks]) {
      if (!Number.isFinite(level.price) || !Number.isFinite(level.quantity) || !(level.price > 0) || !(level.quantity >= 0) || level.price < view.min || level.price > view.max) continue
      const y = replayPriceY(level.price, view, layout)
      for (let x = Math.floor(fromX / columnWidth) * columnWidth; x <= Math.max(fromX, endX - 1); x += columnWidth) {
        const key = `${x}:${level.price}`
        if ((cells.get(key)?.quantity ?? -1) < level.quantity) cells.set(key, { x, y, quantity: level.quantity })
      }
    }
  }
  return { cells: [...cells.values()], columnWidth, rowHeight }
}
