import { depthGap, replayQuoteEnd, type ReplayChartLayout, type ReplayPriceView } from '@/features/replay/chartGeometry'
import type { ReplayGap, ReplayQuote, ReplayRange } from '@/features/replay/types'

export interface HeatSource { quotes: ReplayQuote[]; gaps: ReplayGap[]; end: number }
export interface HeatQuery { range: ReplayRange; view: ReplayPriceView; layout: ReplayChartLayout }
export interface HeatResult { data: Float64Array; rowHeight: number; max: number; known: boolean }

/** Each recording keeps its own interval boundaries and pixel aggregation. */
export function packHeat(sources: HeatSource[]): Float64Array[] {
  return sources.map(({ quotes, gaps, end }) => {
    const rows: number[] = []
    quotes.forEach((quote, index) => {
      if (quote.time_us > end || !quote.book_valid || !quote.available || depthGap(gaps, quote.time_us, quote.symbol)) return
      const until = replayQuoteEnd(quote, quotes[index + 1]?.time_us ?? end, end, gaps)
      for (const level of [...quote.bids, ...quote.asks]) {
        if (Number.isFinite(level.price) && level.price > 0) rows.push(quote.time_us, until, level.price, level.quantity ?? NaN)
      }
    })
    return new Float64Array(rows)
  })
}

export function heatResult(data: Float64Array, query: HeatQuery): HeatResult {
  let max = 0, known = false
  for (let i = 0; i < data.length; i += 6) if (!Number.isNaN(data[i + 2])) { known = true; max = Math.max(max, data[i + 2]) }
  return { data, max, known, rowHeight: Math.max(2, Math.min(18, (query.layout.bottom - query.layout.top) * query.view.tick / (query.view.max - query.view.min))) }
}
