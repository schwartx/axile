import type { PerformancePoint } from '@/types/api'
import { chartTime } from '@/features/history/performance'
import { executionsOnDay, shanghaiTime, summarizeCosts, type CostExecution } from '@/features/history/costs'
import type { AnalysisExecutionSummary } from '@/lib/api/performance'

export type ChartSelection = { kind: 'day'; day: string; time: number }
  | { kind: 'execution'; recordId: number; time: number }
  | { kind: 'interval'; start: number; end: number } | null
export type Viewport = { start: number; end: number }
export const pointTime = (point: PerformancePoint) => point.observed_at ? shanghaiTime(point.observed_at) : chartTime(point.date)
export const precisePoints = (points: PerformancePoint[]) => points.length >= 2 && points.every(p => p.observed_at && Number.isFinite(pointTime(p)))
export const timeLabel = (time: number) => new Date(time + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ')
export const selectionLabel = (selection: ChartSelection) => !selection ? '' : selection.kind === 'execution' ? timeLabel(selection.time) : selection.kind === 'day' ? selection.day : `${timeLabel(selection.start)} → ${timeLabel(selection.end)}`

/** Navigation follows observed and execution times; transaction details load only after selection. */
export function navigationTimes(points: PerformancePoint[], executions: AnalysisExecutionSummary[]): number[] {
  return [...new Set([...points.map(pointTime), ...executions.map(row => row.time_ms)].filter(Number.isFinite))].sort((a, b) => a - b)
}

export function nearestIndex(times: number[], time: number): number {
  if (!times.length) return -1
  let low = 0, high = times.length - 1
  while (low < high) {
    const mid = (low + high) >>> 1
    if (times[mid] < time) low = mid + 1
    else high = mid
  }
  return low > 0 && time - times[low - 1] <= times[low] - time ? low - 1 : low
}

export function clampViewport(view: Viewport, times: number[]): Viewport {
  if (times.length < 2) return { start: times[0] ?? 0, end: (times[0] ?? 0) + 1 }
  const first = times[0], last = times[times.length - 1]
  if (last <= first) return { start: first, end: first + 1 }
  let span = Math.min(last - first, Math.max(1, view.end - view.start))
  let start = Math.max(first, Math.min(last - span, view.start))
  const a = nearestIndex(times, start), b = nearestIndex(times, start + span)
  if (a === b || times[a] < start || times[b] > start + span) {
    const inside = times.filter(t => t >= start && t <= start + span)
    if (inside.length < 2) {
      const left = Math.max(0, Math.min(times.length - 2, nearestIndex(times, start)))
      span = Math.max(span, times[left + 1] - times[left])
      start = Math.max(first, Math.min(last - span, times[left]))
    }
  }
  return { start, end: start + span }
}

export function zoomViewport(view: Viewport, factor: number, anchor: number, times: number[]): Viewport {
  const fraction = Math.max(0, Math.min(1, (anchor - view.start) / (view.end - view.start)))
  const span = (view.end - view.start) * factor
  return clampViewport({ start: anchor - span * fraction, end: anchor + span * (1 - fraction) }, times)
}

export function intervalSelection(a: number, b: number): ChartSelection {
  return a === b ? null : { kind: 'interval', start: Math.min(a, b), end: Math.max(a, b) }
}

/** Binding periods are half-open; never include the next portfolio's observation. */
export function bindingSelection(times: number[], start: number, end: number, includeEnd = false): ChartSelection {
  const inside = times.filter(time => time >= start && (includeEnd ? time <= end : time < end))
  return inside.length < 2 ? null : intervalSelection(inside[0], inside[inside.length - 1])
}

export function reconcileSelection(selection: ChartSelection, points: PerformancePoint[], executions?: AnalysisExecutionSummary[]): ChartSelection {
  if (!selection) return null
  if (selection.kind === 'execution') {
    if (!executions) return selection
    const row = executions.find(row => row.record_id === selection.recordId)
    return !row ? null : row.time_ms === selection.time ? selection : { ...selection, time: row.time_ms }
  }
  const times = points.map(pointTime)
  if (selection.kind === 'day') return points.some(p => p.date.slice(0, 10) === selection.day && pointTime(p) === selection.time) ? selection : null
  return precisePoints(points) && times.includes(selection.start) && times.includes(selection.end) ? selection : null
}

export function intervalReturn(points: PerformancePoint[], selection: ChartSelection, key: 'account_return' | 'portfolio_return' | 'target_portfolio_return'): number | null {
  if (selection?.kind !== 'interval') return null
  const start = points.findIndex(p => pointTime(p) === selection.start), end = points.findIndex(p => pointTime(p) === selection.end)
  if (start < 0 || end <= start) return null
  const a = points[start][key], b = points[end][key]
  if (a == null || b == null || !Number.isFinite(a) || !Number.isFinite(b) || 1 + a <= 0) return null
  if (key !== 'account_return' && points.slice(start, end + 1).some(p => p[key] == null)) return null
  return (1 + b) / (1 + a) - 1
}

export function selectedExecutions(executions: CostExecution[], selection: ChartSelection): CostExecution[] {
  if (!selection) return executions
  if (selection.kind === 'execution') return executions.filter(e => e.record.id === selection.recordId)
  if (selection.kind === 'day') return executionsOnDay(executions, selection.day)
  const inside = (time: number) => time > selection.start && time <= selection.end
  return executions.flatMap(e => {
    const trades = e.trades.filter(t => inside(t.time))
    return trades.length || inside(shanghaiTime(e.record.created_at))
      ? [{ ...e, trades, summary: summarizeCosts(trades) }] : []
  })
}
