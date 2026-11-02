import { currentReplayQuote, defaultReplayPriceView, replayOnBookOrder, replayPriceY, type ReplayChartLayout, type ReplayPriceView } from '@/features/replay/chartGeometry'
import type { ReplayGap, ReplayLevel, ReplayOrder, ReplayProjection, ReplayQuote, ReplayTrade } from '@/features/replay/types'

export interface RecordedDepthLevel extends ReplayLevel { side: '买' | '卖'; rank: number | null }

/** 只为真实观察到的报价编号；零量保留显示，不当作有效最优价。 */
export function recordedDepth(quote: ReplayQuote | null): RecordedDepthLevel[] {
  if (!quote) return []
  return (['买', '卖'] as const).flatMap(side => {
    let rank = 0
    return [...(side === '买' ? quote.bids : quote.asks)].sort((a, b) => side === '买' ? b.price - a.price : a.price - b.price)
      .map(level => ({ ...level, side, rank: level.quantity === 0 ? null : ++rank }))
  })
}

export function replayBestLevel(quote: ReplayQuote, side: '买' | '卖'): RecordedDepthLevel | undefined {
  return recordedDepth(quote).find(level => level.side === side && level.rank === 1)
}

export function replayObservedPriceView(quotes: ReplayQuote[], orders: ReplayOrder[], trades: ReplayTrade[], tick?: number | null): ReplayPriceView | null {
  const levels = quotes.flatMap(quote => [...quote.bids, ...quote.asks])
  levels.push(...[...orders, ...trades].flatMap(row => row.price != null && row.price > 0 ? [{ price: row.price, quantity: null }] : []))
  return levels.length ? defaultReplayPriceView({ bids: levels, asks: [] }, [], tick) : null
}

export interface ReplayDepthBucket {
  minPrice: number; maxPrice: number; y: number; labelY: number
  levels: RecordedDepthLevel[]; orders: ReplayOrder[]
  quantity: number | null; unknown: boolean
}

function depthPriceRows(levels: RecordedDepthLevel[], orders: ReplayOrder[], view: ReplayPriceView) {
  const prices = new Map<number, { levels: RecordedDepthLevel[]; orders: ReplayOrder[] }>()
  const atPrice = (price: number) => {
    const row = prices.get(price) ?? { levels: [], orders: [] }
    prices.set(price, row)
    return row
  }
  for (const level of levels) if (level.price >= view.min && level.price <= view.max) atPrice(level.price).levels.push(level)
  for (const order of orders) if (order.price != null && order.price >= view.min && order.price <= view.max) atPrice(order.price).orders.push(order)
  return prices
}

function positionDepthLabels(buckets: ReplayDepthBucket[], gap: number, layout: ReplayChartLayout) {
  for (let index = 0; index < buckets.length; index++) buckets[index].labelY = Math.max(layout.top + gap / 2, buckets[index].y, (buckets[index - 1]?.labelY ?? layout.top - gap / 2) + gap)
  for (let index = buckets.length - 1; index >= 0; index--) buckets[index].labelY = Math.min(buckets[index].labelY, index === buckets.length - 1 ? layout.bottom - gap / 2 : buckets[index + 1].labelY - gap)
}

/** 拥挤时明确按线性价格区间聚合；标签引导线不改变柱条的真实坐标。 */
export function replayDepthBuckets(levels: RecordedDepthLevel[], orders: ReplayOrder[], view: ReplayPriceView, layout: ReplayChartLayout): ReplayDepthBucket[] {
  const labelLayout = { ...layout, top: Math.max(28, layout.top) }
  const prices = depthPriceRows(levels, orders, view), height = Math.max(1, labelLayout.bottom - labelLayout.top)
  const individual = prices.size <= Math.max(1, Math.floor(height / 18)), gap = individual ? 18 : 26
  const capacity = Math.max(1, Math.floor(height / gap))
  const grouped = new Map<number, ReplayDepthBucket>()
  for (const [price, row] of [...prices].sort(([a], [b]) => b - a)) {
    const y = replayPriceY(price, view, layout)
    const key = individual ? price : Math.min(capacity - 1, Math.floor((y - layout.top) / height * capacity))
    const bucket = grouped.get(key) ?? { minPrice: price, maxPrice: price, y, labelY: y, levels: [], orders: [], quantity: null, unknown: false }
    bucket.minPrice = Math.min(price, bucket.minPrice); bucket.maxPrice = Math.max(price, bucket.maxPrice)
    bucket.levels.push(...row.levels); bucket.orders.push(...row.orders)
    grouped.set(key, bucket)
  }
  const buckets = [...grouped.values()]
  for (const bucket of buckets) {
    const quantities = bucket.levels.flatMap(level => level.quantity == null ? [] : [level.quantity])
    bucket.quantity = quantities.length ? Math.max(...quantities) : null
    bucket.unknown = bucket.levels.some(level => level.quantity == null)
    bucket.y = replayPriceY((bucket.minPrice + bucket.maxPrice) / 2, view, layout)
  }
  positionDepthLabels(buckets, gap, labelLayout)
  return buckets
}

function recordedPriceRange(price: number, levels: RecordedDepthLevel[], side: '买' | '卖'): string {
  const own = levels.filter(level => level.side === side)
  if (!own.length) return `未录到${side}方价位`
  const prices = own.map(level => level.price)
  return price > Math.max(...prices) ? `高于已录${side}价区间` : price < Math.min(...prices) ? `低于已录${side}价区间` : '已录价位之间，未录到同价档'
}

export function replayOrderBookPosition(order: ReplayOrder, quote: ReplayQuote | null): string {
  if (order.price == null || !quote || !['BUY', 'SELL'].includes(order.direction ?? '')) return '盘口未知，无法判断价位'
  const side = order.direction === 'BUY' ? '买' : '卖', opposite = side === '买' ? '卖' : '买'
  const levels = recordedDepth(quote), exact = levels.find(level => level.price === order.price && level.side === side)
  if (exact) return exact.rank == null ? `已录${side}价位，挂量为零` : `已录${side}${exact.rank}${exact.quantity == null ? ' · 挂量未知' : ''}`
  const other = levels.find(level => level.price === order.price && level.side === opposite && level.rank != null)
  if (other) return `对手${opposite}${other.rank}价位`
  const bestBid = levels.find(level => level.side === '买' && level.rank === 1), bestAsk = levels.find(level => level.side === '卖' && level.rank === 1)
  if (bestBid && bestAsk && order.price > bestBid.price && order.price < bestAsk.price) return '买一与卖一之间'
  return recordedPriceRange(order.price, levels, side)
}

export function replayOrderPositionContext(order: ReplayOrder, projection: ReplayProjection, gaps: ReplayGap[]): { label: string; time: number | null } {
  const historical = !replayOnBookOrder(order)
  const row = historical ? projection.orderHistory.findLast(value => value.key === order.key && replayOnBookOrder(value)) : order
  if (!row) return { label: '挂单时盘口未记录', time: null }
  const time = historical ? row.time_us : projection.time_us
  const quote = historical ? projection.quoteHistory.findLast(value => value.symbol === order.symbol && value.time_us <= time && value.capture_seq <= row.capture_seq) : projection.quotes[order.symbol]
  return { label: `${historical ? '挂单时' : '当前'}：${replayOrderBookPosition(row, currentReplayQuote(quote, time, gaps))}`, time }
}

export function replayTradeLiquidity(trade: Pick<ReplayTrade, 'attributes' | 'conflict'>): string {
  const quality = trade.attributes.field_quality
  const state = quality && typeof quality === 'object' ? (quality as Record<string, unknown>).is_maker : undefined
  if (trade.conflict || state === 'missing' || state === 'invalid') return '主被动未知'
  return trade.attributes.is_maker === true ? '被动成交' : trade.attributes.is_maker === false ? '主动成交' : '主被动未知'
}
