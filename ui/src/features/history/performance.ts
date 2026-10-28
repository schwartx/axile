import type { PerformancePoint, PerformanceSettings } from '@/types/api'
export { niceValueAxis as niceReturnAxis } from '@/components/viz/chartAxis'
import { shanghaiTime } from './costs'

export const WEIGHT_MODES: Array<{ value: 'ts' | 'cs'; label: string }> = [
  { value: 'ts', label: '时序' }, { value: 'cs', label: '截面' },
]

export const FEE_PRESETS = ['0', '5', '15', '30'] as const
export const FEE_OPTIONS = [...FEE_PRESETS.map(value => ({ value, label: value })), { value: 'custom', label: '自定义' }]

export function feeOption(fee: string): string {
  return fee.trim() && FEE_PRESETS.some(value => Number(value) === Number(fee)) ? String(Number(fee)) : 'custom'
}

export function returnColor(value: number | null | undefined): string {
  return value == null || value === 0 ? 'text-ink-1' : value > 0 ? 'text-up' : 'text-down'
}

export function axisPercent(value: number, step: number): string {
  const digits = Math.max(1, Math.min(10, Math.ceil(-Math.log10(Math.abs(step) * 100))))
  const rounded = Number((value * 100).toFixed(digits))
  return `${(rounded === 0 ? 0 : rounded).toFixed(digits)}%`
}

export function timeTicks(points: PerformancePoint[], x: (i: number) => number) {
  if (!points.length) return []
  const last = points.length - 1
  const sameDay = points[0].date.slice(0, 10) === points[last].date.slice(0, 10)
  const monthly = chartTime(points[last].date) - chartTime(points[0].date) > 90 * 864e5
  const candidates = sameDay ? [0, last] : [0, ...points.flatMap((p, i) => i > 0 && (monthly ? p.date.slice(0, 7) !== points[i - 1].date.slice(0, 7) : new Date(`${p.date.slice(0, 10)}T12:00:00Z`).getUTCDay() === 1) ? [i] : []), last]
  let previous = -Infinity
  const chosen = [...new Set(candidates)].filter(i => {
    if (i !== last && (x(i) - previous < 140 || (i !== 0 && x(last) - x(i) < 140))) return false
    previous = x(i)
    return true
  })
  return chosen.map(i => ({
    index: i,
    label: sameDay ? (points[i].date.includes('T') ? points[i].date.slice(11, 16) : '日末') : points[i].date.slice(0, 10),
  }))
}

export function settingsFromDraft(mode: 'ts' | 'cs', feeBp: string): PerformanceSettings | null {
  if (!feeBp.trim()) return null
  const fee = Number(feeBp)
  if (!Number.isFinite(fee) || fee < 0 || fee >= 10000) return null
  return { backtest_weight_type: mode, backtest_fee_rate: fee / 10000 }
}

export function returnText(value: number | null | undefined, unit = '%'): string {
  return value == null ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}${unit}`
}

export function chartTime(date: string): number {
  // 日收益点放在上海日末，共同基准保留实际执行时间。
  return shanghaiTime(date.includes('T') || date.includes(' ') ? date : `${date}T23:59:59`)
}

export function returnPaths(points: PerformancePoint[], key: keyof PerformancePoint, x: (i: number) => number, y: (v: number) => number): string {
  let path = ''
  let connected = false
  points.forEach((point, i) => {
    const value = point[key]
    if (typeof value !== 'number') { connected = false; return }
    path += `${connected ? 'L' : 'M'}${x(i)},${y(value)} `
    connected = true
  })
  return path
}
