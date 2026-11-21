import { pointTime, type Viewport } from './chartModel'
import { createTimeScale, type TimeScaleMode } from './timeScale'
import type { AnalysisChart } from '@/lib/api/performance'
import type { ContributionGroup } from './contributions'

export type RangeCurve = Pick<ContributionGroup, 'symbol' | 'product' | 'values' | 'baselines'>

export interface RangeTarget {
  chart: AnalysisChart
  finish: () => void
}

export function rangeZoom(from: Viewport, to: Viewport, times: number[], mode: TimeScaleMode) {
  if (from.end !== to.end || from.start === to.start || to.start >= to.end) return null
  const scale = createTimeScale(mode, times)
  const left = scale.project(from.start), distance = scale.project(to.start) - left
  const domain = { start: Math.min(from.start, to.start), end: to.end }
  return { scale, domain, viewport: (progress: number) => ({ start: scale.invert(left + distance * progress), end: to.end }) }
}

/** Show overlapping observations on the old basis until the target range is committed. */
export function rangeReturnPoints(before: AnalysisChart['result']['points'], after: AnalysisChart['result']['points']) {
  const old = new Map(before.map(point => [pointTime(point), point]))
  const shared = after.find(point => old.has(pointTime(point)))
  const previous = shared && old.get(pointTime(shared))
  if (!shared || !previous) return before
  const keys = ['account_return', 'portfolio_return', 'target_portfolio_return'] as const
  const added = after.filter(point => !old.has(pointTime(point))).map(point => {
    const result = { ...point }
    for (const key of keys) {
      const value = point[key], base = shared[key], oldValue = previous[key]
      result[key] = value != null && base != null && base !== -1 && oldValue != null
        ? (1 + value) * ((1 + oldValue) / (1 + base)) - 1 : null
    }
    return result
  })
  return added.length ? [...before, ...added].sort((a, b) => pointTime(a) - pointTime(b)) : before
}

export function rangeExecutions(before: AnalysisChart['execution_index'], after: AnalysisChart['execution_index']) {
  const rows = new Map(after.map(row => [row.record_id, row]))
  before.forEach(row => rows.set(row.record_id, row))
  return [...rows.values()]
}

/** Keep the old segment values. Extend its matching segment on the same P&L basis. */
export function rangeContributionRows(before: RangeCurve[], after: RangeCurve[], oldTimes: number[], newTimes: number[]) {
  const old = new Map(oldTimes.map((time, i) => [time, i]))
  const incoming = new Map(newTimes.map((time, i) => [time, i]))
  const times = [...new Set([...oldTimes, ...newTimes])].sort((a, b) => a - b)
  const rows = before.map(row => {
    const next = after.find(curve => curve.symbol === row.symbol)
    const links = new Map<number, { origin: number; offset: number }>()
    if (next) newTimes.forEach((time, i) => {
      const j = old.get(time), nextBase = next.baselines[i], nextValue = next.values[i]
      if (j == null || nextBase == null || nextValue == null || links.has(nextBase)) return
      const origin = row.baselines[j], value = row.values[j]
      if (origin != null && value != null) links.set(nextBase, { origin, offset: value - nextValue })
    })
    const segments = new Map<number, number>()
    const baselines: (number | null)[] = []
    const values = times.map((time, index) => {
      const i = old.get(time), j = incoming.get(time)
      const nextBase = j == null ? null : next?.baselines[j]
      const link = nextBase == null ? undefined : links.get(nextBase)
      const base = i == null ? link?.origin ?? (nextBase == null ? null : -1 - nextBase) : row.baselines[i]
      if (base != null && !segments.has(base)) segments.set(base, index)
      baselines.push(base == null ? null : segments.get(base)!)
      if (i != null) return row.values[i]
      const value = j == null ? null : next?.values[j]
      return value == null ? null : value + (link?.offset ?? 0)
    })
    return { ...row, values, baselines }
  })
  return { times, rows }
}
