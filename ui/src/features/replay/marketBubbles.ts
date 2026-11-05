import { replayTimeX, type ReplayChartLayout } from '@/features/replay/chartGeometry'
import { replayFutureRecord } from '@/features/replay/inspection'
import type { ReplayCursor, ReplayGap, ReplayRange, ReplayTrade } from '@/features/replay/types'

export interface ReplayMarketBubble {
  price: number
  quantity: number
  time_us: number
  end_us: number
  direction: string | null
  trades: ReplayTrade[]
  stable: boolean
}

interface ReplayMarketBubbleGroup {
  bubble: ReplayMarketBubble
  first: ReplayTrade
  last: ReplayTrade
  positions: number[]
}

const compareTrades = (left: ReplayTrade, right: ReplayTrade) => left.time_us - right.time_us || left.capture_seq - right.capture_seq

function appendTrade(group: ReplayMarketBubble | undefined, trade: ReplayTrade): ReplayMarketBubble {
  const stable = trade.identity_quality === 'stable' && !trade.conflict
  if (!group) return { price: trade.price!, quantity: trade.quantity!, time_us: trade.time_us, end_us: trade.time_us, direction: trade.direction, trades: [trade], stable }
  group.quantity += trade.quantity!; group.trades.push(trade)
  group.time_us = Math.min(group.time_us, trade.time_us); group.end_us = Math.max(group.end_us, trade.time_us)
  group.stable &&= stable
  return group
}

/** 静态按时间像素、真实价格、方向和缺口分组，不随游标重新扫描全部成交。 */
export function replayMarketBubbleGroups(trades: ReplayTrade[], symbol: string, range: ReplayRange, layout: ReplayChartLayout, gaps: ReplayGap[]): ReplayMarketBubbleGroup[] {
  const groups = new Map<string, ReplayMarketBubbleGroup>()
  const boundaries = gaps.filter(gap => {
    const topic = gap.topic?.replace(/^\/+/, '')
    return (!gap.symbol || gap.symbol === symbol) && (!topic || topic === 'market_trade' || topic.startsWith('market_trade/'))
  })
    .flatMap(gap => [gap.start_us, gap.end_us]).sort((a, b) => a - b)
  trades.forEach((trade, position) => {
    if (trade.symbol !== symbol || trade.time_us < range.start_us || trade.time_us >= range.end_us || trade.price == null || !Number.isFinite(trade.price) || trade.quantity == null || !Number.isFinite(trade.quantity) || trade.quantity <= 0) return
    const key = JSON.stringify([Math.floor(replayTimeX(trade.time_us, range, layout)), trade.price, trade.direction, boundaries.filter(time => time <= trade.time_us).length])
    const group = groups.get(key)
    if (group) {
      appendTrade(group.bubble, trade); group.positions.push(position)
      if (compareTrades(trade, group.first) < 0) group.first = trade
      if (compareTrades(trade, group.last) > 0) group.last = trade
    } else groups.set(key, { bubble: appendTrade(undefined, trade), first: trade, last: trade, positions: [position] })
  })
  return [...groups.values()]
}

/** 只有游标穿过的分组才拆分；两侧沿用完整聚合及原始记录顺序。 */
export function replayMarketBubblesAt(groups: ReplayMarketBubbleGroup[], cursor: ReplayCursor): ReplayMarketBubble[] {
  const result: { bubble: ReplayMarketBubble; position: number }[] = []
  let split = false
  for (const group of groups) {
    if (replayFutureRecord(group.first.time_us, group.first.capture_seq, cursor) === replayFutureRecord(group.last.time_us, group.last.capture_seq, cursor)) {
      result.push({ bubble: group.bubble, position: group.positions[0] })
      continue
    }
    split = true
    const sides: ({ bubble: ReplayMarketBubble; position: number } | undefined)[] = []
    group.bubble.trades.forEach((trade, index) => {
      const side = Number(replayFutureRecord(trade.time_us, trade.capture_seq, cursor)), previous = sides[side]
      if (previous) appendTrade(previous.bubble, trade)
      else sides[side] = { bubble: appendTrade(undefined, trade), position: group.positions[index] }
    })
    for (const side of sides) if (side) result.push(side)
  }
  if (split) result.sort((a, b) => a.position - b.position)
  return result.map(item => item.bubble)
}

/** 同一时间像素、真实价格和主动方向聚合；缺口及游标两侧仍分开保留原始记录。 */
export function replayMarketBubbles(trades: ReplayTrade[], symbol: string, range: ReplayRange, layout: ReplayChartLayout, cursor: ReplayCursor, gaps: ReplayGap[]): ReplayMarketBubble[] {
  return replayMarketBubblesAt(replayMarketBubbleGroups(trades, symbol, range, layout, gaps), cursor)
}

/** 当前视野内按量缩放面积，最小两像素保证小成交可见。 */
export const replayBubbleRadius = (quantity: number, maximum: number) => Math.max(2, 14 * Math.sqrt(quantity / maximum))
