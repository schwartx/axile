import { replayTimeX, type ReplayChartLayout } from '@/features/replay/chartGeometry'
import type { ReplayCursor, ReplayGap, ReplayRange, ReplayTrade } from '@/features/replay/types'

export interface ReplayMarketBubble {
  price: number
  quantity: number
  time_us: number
  end_us: number
  direction: string | null
  trades: ReplayTrade[]
}

/** 同一时间像素、真实价格和主动方向聚合；缺口及游标两侧仍分开保留原始记录。 */
export function replayMarketBubbles(trades: ReplayTrade[], symbol: string, range: ReplayRange, layout: ReplayChartLayout, cursor: ReplayCursor, gaps: ReplayGap[]): ReplayMarketBubble[] {
  const groups = new Map<string, ReplayMarketBubble>()
  const boundaries = gaps.filter(gap => {
    const topic = gap.topic?.replace(/^\/+/, '')
    return (!gap.symbol || gap.symbol === symbol) && (!topic || topic === 'market_trade' || topic.startsWith('market_trade/'))
  })
    .flatMap(gap => [gap.start_us, gap.end_us]).sort((a, b) => a - b)
  for (const trade of trades) {
    if (trade.symbol !== symbol || trade.time_us < range.start_us || trade.time_us >= range.end_us || trade.price == null || !Number.isFinite(trade.price) || trade.quantity == null || !Number.isFinite(trade.quantity) || trade.quantity <= 0) continue
    const future = trade.time_us > cursor.time_us || trade.time_us === cursor.time_us && cursor.capture_seq != null && trade.capture_seq > cursor.capture_seq
    const key = JSON.stringify([Math.floor(replayTimeX(trade.time_us, range, layout)), trade.price, trade.direction, boundaries.filter(time => time <= trade.time_us).length, future])
    const group = groups.get(key)
    if (group) {
      group.quantity += trade.quantity; group.trades.push(trade)
      group.time_us = Math.min(group.time_us, trade.time_us); group.end_us = Math.max(group.end_us, trade.time_us)
    } else groups.set(key, { price: trade.price, quantity: trade.quantity, time_us: trade.time_us, end_us: trade.time_us, direction: trade.direction, trades: [trade] })
  }
  return [...groups.values()]
}

/** 当前视野内按量缩放面积，最小两像素保证小成交可见。 */
export const replayBubbleRadius = (quantity: number, maximum: number) => Math.max(2, 14 * Math.sqrt(quantity / maximum))
