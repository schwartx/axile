import type { AnalysisExecutionSummary, CostExecutionRow, TransactionPreview } from '@/lib/api/performance'
import type { ChartScene } from '@/components/viz/performanceCanvas'
import { chartAxis, PLOT, plotRight, returnY, xPosition } from '@/components/viz/performanceCanvas'
import { shanghaiTime } from '@/features/history/costs'

export interface TradeAnnotation { row: CostExecutionRow; trade?: TransactionPreview; time: number; side: 'buy' | 'sell' | 'none' | 'warning' }
export interface AnnotationGroup { key: string; x: number; y: number; time: number; endTime: number; anchorTime: number; side: TradeAnnotation['side']; items: TradeAnnotation[] }

export function executionAnnotations(rows: CostExecutionRow[]): TradeAnnotation[] {
  return rows.flatMap(row => {
    const annotations: TradeAnnotation[] = (row.transactions ?? []).map(trade => ({ row, trade, time: trade.time, side: trade.side }))
    if (row.record.is_success !== 1 || ['PARTIAL', 'TERMINATED'].includes(row.record.raw_result.task_status ?? '') || row.record.raw_result.status === 'PARTIAL') {
      annotations.push({ row, time: shanghaiTime(row.record.created_at), side: 'warning' })
    }
    return annotations
  }).sort((a, b) => a.time - b.time || a.row.record.id - b.row.record.id)
}

/** Use line geometry only to place annotations; never publish interpolated returns. */
function annotationY(scene: ChartScene, time: number): number {
  const { times, data } = scene
  const right = times.findIndex(t => t >= time)
  const index = right < 0 ? times.length - 1 : right
  const key = scene.daily ? 'account_daily_return' : 'account_return'
  const b = data.points[index]?.[key]
  const a = data.points[Math.max(0, index - 1)]?.[key]
  if (b == null || a == null) return (PLOT.top + PLOT.bottom) / 2
  const start = times[Math.max(0, index - 1)]
  const ratio = Math.max(0, Math.min(1, (time - start) / (times[index] - start || 1)))
  return returnY(a + (b - a) * ratio, chartAxis(scene))
}

export function groupAnnotations(annotations: TradeAnnotation[], scene: ChartScene): AnnotationGroup[] {
  const groups = new Map<string, TradeAnnotation[]>()
  const spacing = 64
  const bucket = (scene.viewport.end - scene.viewport.start) / Math.max(1, plotRight(scene.width) - PLOT.left) * spacing
  for (const item of annotations) {
    if (item.time < scene.viewport.start || item.time > scene.viewport.end) continue
    const key = `${Math.floor(item.time / Math.max(1, bucket))}:${item.side}`
    const items = groups.get(key) ?? []
    items.push(item); groups.set(key, items)
  }
  return Array.from(groups, ([key, items]) => {
    const time = items[0].time
    const endTime = Math.max(...items.map(i => i.trade?.endTime ?? i.time))
    const center = (time + items.at(-1)!.time) / 2
    const side = items[0].side
    const offset = side === 'buy' ? 25 : side === 'sell' ? -25 : -53
    return { key, items, time, endTime, anchorTime: center, side,
      x: Math.max(PLOT.left + 12, Math.min(plotRight(scene.width) - 12, xPosition(center, scene.width, scene.viewport))),
      y: Math.max(PLOT.top + 16, Math.min(PLOT.bottom - 16, annotationY(scene, center) + offset)),
    }
  })
}

export function aggregateTransactions(items: TradeAnnotation[]): TransactionPreview[] {
  const grouped = new Map<string, TransactionPreview[]>()
  for (const { trade } of items) {
    if (!trade) continue
    const key = `${trade.symbol}:${trade.side}`
    grouped.set(key, [...(grouped.get(key) ?? []), trade])
  }
  return Array.from(grouped.values(), trades => {
    const quantity = trades.reduce((sum, t) => sum + t.quantity, 0)
    const summaries = trades.map(t => t.summary)
    const sum = (values: (number | null)[]) => values.every(v => v == null) ? null : values.reduce<number>((n, v) => n + (v ?? 0), 0)
    const value = sum(summaries.map(s => s.value))
    const weighted = summaries.map(s => ({ bp: s.lossBp, value: s.value != null && s.coverage != null ? s.value * s.coverage : 0 }))
    const coveredValue = weighted.reduce((n, s) => n + (s.bp == null ? 0 : s.value), 0)
    const fees: Record<string, number> = {}
    summaries.forEach(s => Object.entries(s.fees).forEach(([currency, fee]) => { fees[currency] = (fees[currency] ?? 0) + fee }))
    return { ...trades[0], quantity, before: null, target: null,
      price: trades.every(t => t.price != null) ? trades.reduce((n, t) => n + t.price! * t.quantity, 0) / quantity : null,
      reference: null, referenceSource: null,
      time: Math.min(...trades.map(t => t.time)), endTime: Math.max(...trades.map(t => t.endTime)),
      timeEstimated: trades.some(t => t.timeEstimated),
      summary: { value, cost: sum(summaries.map(s => s.cost)),
        lossBp: coveredValue > 0 ? weighted.reduce((n, s) => n + (s.bp ?? 0) * s.value, 0) / coveredValue : null,
        coverage: value && summaries.every(s => s.amountComplete) ? coveredValue / value : null,
        count: summaries.reduce((n, s) => n + s.count, 0), covered: summaries.reduce((n, s) => n + s.covered, 0),
        amountComplete: summaries.every(s => s.amountComplete), fees, feeCovered: summaries.reduce((n, s) => n + s.feeCovered, 0),
      },
    }
  })
}

/** At-or-before is essential: hovering between records must not reveal future holdings. */
export function holdingAt(rows: CostExecutionRow[], time: number): CostExecutionRow | null {
  let low = 0, high = rows.length
  while (low < high) {
    const mid = (low + high) >>> 1
    if (shanghaiTime(rows[mid].record.created_at) <= time) low = mid + 1
    else high = mid
  }
  return rows[low - 1] ?? null
}

/** Latest summary at the cursor, including same-time records in their stable record-ID order. */
export function executionAt(rows: AnalysisExecutionSummary[], time: number): AnalysisExecutionSummary | null {
  let low = 0, high = rows.length
  while (low < high) {
    const mid = (low + high) >>> 1
    if (rows[mid].time_ms <= time) low = mid + 1
    else high = mid
  }
  return rows[low - 1] ?? null
}

export function positionDirection(direction: string, quantity: number): string {
  if (quantity < 0 || /short|空/i.test(direction)) return '空'
  if (/long|多/i.test(direction)) return '多'
  return direction || '方向未知'
}
