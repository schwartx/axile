import { depthGap, replayQuoteEnd } from './chartGeometry'
import { replayInstrument, replayStream } from './presentation'
import type { ReplayExecutionSlice } from './executionScene'
import type { ReplayTrade, ReplayRange } from './types'

export type IntervalSlice = Pick<ReplayExecutionSlice, 'range' | 'window' | 'scene' | 'tradeFacts'>

function intervalTrades(slices: IntervalSlice[], symbol: string, stream: 'marketTrades' | 'ownTrades') {
  const admitted = new Set<string>(), resolved = new Set<string>()
  const facts = new Map<string, { recording: string; key: string | null; time: number; price: number | null; quantity: number | null; stable: boolean; conflict: boolean }>()
  const identity = (recording: string, value: ReplayTrade) => JSON.stringify([recording, value.key == null ? 'observation' : 'trade', value.key ?? value.capture_seq])
  for (const slice of slices.toSorted((a, b) => a.range.end_us - b.range.end_us)) {
    const recording = slice.window.recording_id
    if (stream !== 'marketTrades' || replayStream(slice.window.context, symbol, 'market_trade')?.availability !== 'unavailable') {
      for (const value of slice.scene[stream]) if (value.symbol === symbol) admitted.add(identity(recording, value))
    }
    for (const value of slice.tradeFacts[stream]) {
      if (value.symbol !== symbol) continue
      const key = identity(recording, value), previous = facts.get(key)
      facts.set(key, { recording, key: value.key, time: Math.min(previous?.time ?? value.time_us, value.time_us), price: value.price ?? previous?.price ?? null, quantity: value.quantity ?? previous?.quantity ?? null,
        stable: value.identity_quality === 'stable', conflict: value.conflict || previous?.conflict === true
          || previous?.price != null && value.price != null && previous.price !== value.price
          || previous?.quantity != null && value.quantity != null && previous.quantity !== value.quantity })
      if (value.related_capture_seq != null) resolved.add(JSON.stringify([recording, 'observation', value.related_capture_seq]))
    }
  }
  return [...facts].flatMap(([key, value]) => admitted.has(key) && !resolved.has(key) ? [value] : [])
}

/** Preserve raw side/rank/unknown quantities; heat pixels cannot supply these statistics. */
export function intervalInputs(slices: IntervalSlice[], symbol: string) {
  return {
    depth: slices.flatMap(slice => {
      if (replayStream(slice.window.context, symbol, 'depth')?.availability === 'unavailable') return []
      const quotes = slice.scene.quoteHistory.filter(quote => quote.symbol === symbol)
      return quotes.flatMap((quote, index) => {
        if (!quote.book_valid || !quote.available || depthGap(slice.window.gaps, quote.time_us, symbol)) return []
        const start = Math.max(slice.range.start_us, quote.time_us)
        const end = replayQuoteEnd(quote, quotes[index + 1]?.time_us ?? slice.range.end_us, slice.range.end_us, slice.window.gaps)
        return start < end ? [{ start, end, bids: quote.bids, asks: quote.asks, available_depth: quote.available_depth }] : []
      })
    }).sort((a, b) => a.start - b.start),
    market: intervalTrades(slices, symbol, 'marketTrades'),
    own: intervalTrades(slices, symbol, 'ownTrades'),
  }
}

/** A different recording outside the selection must not change its tick or source quality. */
export function intervalMetadata(slices: IntervalSlice[], symbol: string, range: ReplayRange) {
  const selected = slices.filter(slice => slice.range.start_us < range.end_us && slice.range.end_us > range.start_us)
  const stream = (name: string) => selected.map(slice => replayStream(slice.window.context, symbol, name))
  const ticks = selected.map(slice => replayInstrument(slice.window.context, symbol).price_tick)
  const tick = ticks.length && ticks.every(value => value != null && value > 0 && value === ticks[0]) ? ticks[0]! : null
  const market = stream('market_trade')
  const marketGap = selected.some(slice => slice.window.gaps.some(gap => {
    const topic = gap.topic?.replace(/^\/+/, '')
    return gap.start_us < Math.min(slice.range.end_us, range.end_us) && gap.end_us > Math.max(slice.range.start_us, range.start_us) && (!gap.symbol || gap.symbol === symbol)
      && (!topic || topic === 'market_trade' || topic.startsWith('market_trade/'))
  }))
  let covered = range.start_us
  for (const slice of selected.toSorted((a, b) => a.range.start_us - b.range.start_us)) {
    if (slice.range.start_us > covered) break
    covered = Math.max(covered, slice.range.end_us)
  }
  const availability = market.length && market.every(value => value?.availability === 'unavailable') ? 'unavailable'
    : covered >= range.end_us && !marketGap && market.length && market.every(value => value?.availability === 'available') ? 'available' : 'partial'
  return { tick, availability, sources: [...new Set(market.flatMap(value => value?.source ? [value.source] : []))] }
}
export interface IntervalVolume { count: number; omitted: number; quantity: number; vwap: number | null; profile: [number, number][]; bin_width: number | null }
export interface IntervalStats { spread: number | null; spread_coverage: number; imbalance: number | null; imbalance_coverage: number; market: IntervalVolume; own: IntervalVolume }
