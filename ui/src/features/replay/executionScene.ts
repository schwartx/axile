import { currentReplayQuote } from '@/features/replay/chartGeometry'
import { replayTradeKey } from '@/features/replay/chartInteraction'
import { createReplayProjector } from '@/features/replay/model'
import type { ReplayCursor, ReplayGap, ReplayProjection, ReplayRange, ReplayWindow } from '@/features/replay/types'

export interface ReplayExecutionSlice { range: ReplayRange; window: ReplayWindow; scene: ReplayProjection; tradeFacts: Pick<ReplayProjection, 'ownTrades' | 'marketTrades'>; project: (cursor: ReplayCursor) => ReplayProjection }

function taggedProjector(project: (cursor: ReplayCursor) => ReplayProjection, recordingId: string) {
  let previous: ReplayProjection | undefined, tagged: ReplayProjection | undefined
  return (cursor: ReplayCursor): ReplayProjection => {
    const projection = project(cursor)
    const result = { ...projection,
      ownTrades: previous?.ownTrades === projection.ownTrades ? tagged!.ownTrades : projection.ownTrades.map(trade => ({ ...trade, recording_id: recordingId })),
      marketTrades: previous?.marketTrades === projection.marketTrades ? tagged!.marketTrades : projection.marketTrades.map(trade => ({ ...trade, recording_id: recordingId })),
      orderHistory: previous?.orderHistory === projection.orderHistory ? tagged!.orderHistory : projection.orderHistory.map(order => ({ ...order, recording_id: recordingId })) }
    previous = projection; tagged = result
    return result
  }
}

/** 每段单独投影；窗口基线只在本段的已覆盖范围内绘图。 */
export function replayExecutionScene(parts: readonly { range: ReplayRange; window: ReplayWindow }[], range: ReplayRange, gaps: ReplayGap[]) {
  const slices: ReplayExecutionSlice[] = parts.map(({ range: part, window }) => {
    const project = taggedProjector(createReplayProjector(window), window.recording_id)
    const projection = project({ time_us: part.end_us - 1 })
    const quoteHistory = projection.quoteHistory.flatMap(quote => {
      if (quote.time_us >= part.end_us) return []
      if (quote.time_us < part.start_us && !currentReplayQuote(quote, part.start_us, window.gaps)) return []
      return [{ ...quote, recording_id: window.recording_id, time_us: Math.max(quote.time_us, part.start_us),
        valid_until_us: quote.valid_until_us == null ? null : Math.min(quote.valid_until_us, part.end_us) }]
    })
    const inRange = (time: number) => time >= part.start_us && time < part.end_us
    return { range: part, window, project, tradeFacts: { ownTrades: projection.ownTrades, marketTrades: projection.marketTrades }, scene: { ...projection, quoteHistory,
      ownTrades: projection.ownTrades.filter(trade => inRange(trade.time_us)),
      marketTrades: projection.marketTrades.filter(trade => inRange(trade.time_us)) } }
  })
  const first = slices[0]
  if (!first) return null
  const chronological = <T extends { time_us: number; capture_seq: number }>(rows: T[]) => rows.sort((a, b) => a.time_us - b.time_us || a.capture_seq - b.capture_seq)
  const orders: ReplayProjection['orders'] = Object.assign({}, ...slices.map(slice => slice.scene.orders))
  const scene = { ...first.scene, recording_id: '', time_us: range.end_us - 1, capture_seq: Number.MAX_SAFE_INTEGER, orders,
    quoteHistory: chronological(slices.flatMap(slice => slice.scene.quoteHistory)),
    orderHistory: chronological(slices.flatMap(slice => slice.scene.orderHistory)),
    ownTrades: chronological(slices.flatMap(slice => slice.scene.ownTrades)),
    marketTrades: chronological(slices.flatMap(slice => slice.scene.marketTrades)),
    commands: chronological(slices.flatMap(slice => slice.scene.commands.filter(command => command.time_us >= slice.range.start_us && command.time_us < slice.range.end_us))) }
  return { slices, scene, gaps: [...gaps, ...slices.flatMap(slice => slice.window.gaps)] }
}

const reachedTradesCache = new WeakMap<object, { current: ReplayProjection['ownTrades']; end: number; boundary: string; value: ReplayProjection['ownTrades'] }>()

function reachedTrades(overview: NonNullable<ReturnType<typeof replayExecutionScene>>, projection: ReplayProjection, cursor: ReplayCursor, recordingId?: string) {
  const rows = overview.scene.ownTrades
  let low = 0, high = rows.length
  while (low < high) {
    const middle = (low + high) >>> 1
    if (rows[middle].time_us <= cursor.time_us) low = middle + 1
    else high = middle
  }
  const boundary = cursor.capture_seq == null ? '' : JSON.stringify([cursor.time_us, cursor.capture_seq, recordingId])
  const cached = reachedTradesCache.get(overview)
  if (cached?.current === projection.ownTrades && cached.end === low && cached.boundary === boundary) return cached.value
  const current = new Map(projection.ownTrades.map(trade => [replayTradeKey(trade), trade]))
  const value = rows.slice(0, low).filter(trade => trade.time_us < cursor.time_us || cursor.capture_seq == null
    || trade.recording_id === recordingId && trade.capture_seq <= cursor.capture_seq).map(trade => current.get(replayTradeKey(trade)) ?? trade)
  reachedTradesCache.set(overview, { current: projection.ownTrades, end: low, boundary, value })
  return value
}

/** 缺口和身份不明的重叠段不延用任一段的即时状态。 */
export function replayExecutionAt(overview: NonNullable<ReturnType<typeof replayExecutionScene>>, cursor: ReplayCursor, _symbol: string): ReplayProjection {
  const active = overview.slices.filter(slice => cursor.time_us >= slice.range.start_us && cursor.time_us < slice.range.end_us)
  const selected = active.length === 1 ? active[0] : null
  const baselineSeq = Number(selected?.window.state_before_start?.state_through_seq ?? 0)
  const localCursor = cursor.capture_seq != null && cursor.capture_seq < baselineSeq ? { time_us: cursor.time_us } : cursor
  const projection = selected ? selected.project(localCursor) : {
    ...overview.scene, recording_id: '', time_us: cursor.time_us, capture_seq: 0, quotes: {}, orders: {},
    orderHistory: [], quoteHistory: [], ownTrades: [], marketTrades: [], commands: [], targets: {}, targetsByExecution: {},
    observedPositions: {}, derivedPositions: {}, executionTotals: {}, executionComplete: {}, executionSymbolComplete: {},
    accountingComplete: false, warnings: [active.length ? '录制时段重叠，当前状态无法确定' : '当前时刻未录制'],
  }
  return { ...projection, ownTrades: reachedTrades(overview, projection, cursor, selected?.window.recording_id) }
}
