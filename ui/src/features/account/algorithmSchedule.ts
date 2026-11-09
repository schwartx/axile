import { describeAlgorithmRef, validateAlgorithmRef } from '@/features/setup/algorithms'
import type { AlgorithmRef, AlgorithmTimeRule } from '@/types/api'

export type AlgorithmRuleDraft = AlgorithmTimeRule & { key: string }

export function algorithmScheduleDraft(rules: AlgorithmTimeRule[]): AlgorithmRuleDraft[] {
  return rules.toSorted((a, b) => a.start.localeCompare(b.start))
    .map(rule => ({ ...structuredClone(rule), key: crypto.randomUUID() }))
}

export function algorithmScheduleValue(rules: AlgorithmRuleDraft[]): AlgorithmTimeRule[] {
  return rules.map(({ start, end, algorithm }) => ({ start, end, algorithm }))
    .toSorted((a, b) => a.start.localeCompare(b.start))
}

export function algorithmTimeLabel(start: string, end: string): string {
  if (!start || !end) return '选择时间段'
  return `${start} 至 ${end < start ? '次日 ' : ''}${end}`
}

export function algorithmScheduleErrors(rules: AlgorithmTimeRule[]): (string | null)[] {
  const valid = (time: string) => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time)
  const errors = rules.map(rule => !valid(rule.start) || !valid(rule.end)
    ? '请选择开始和结束时间'
    : rule.start === rule.end ? '开始和结束时间不能相同' : validateAlgorithmRef(rule.algorithm))
  const segments = rules.flatMap((rule, index) => {
    if (!valid(rule.start) || !valid(rule.end) || rule.start === rule.end) return []
    const intervals = rule.start < rule.end ? [[rule.start, rule.end]] : [[rule.start, '24:00'], ['00:00', rule.end]]
    return intervals.filter(([start, end]) => start < end).map(([start, end]) => ({ start, end, index }))
  }).toSorted((a, b) => a.start.localeCompare(b.start) || a.end.localeCompare(b.end))
  for (let i = 1; i < segments.length; i++) {
    const previous = segments[i - 1], current = segments[i]
    if (current.start < previous.end) {
      errors[current.index] = `与 ${algorithmTimeLabel(rules[previous.index].start, rules[previous.index].end)} 重叠`
      errors[previous.index] = `与 ${algorithmTimeLabel(rules[current.index].start, rules[current.index].end)} 重叠`
    }
  }
  return errors
}

export function algorithmParameterSummary(algorithm: AlgorithmRef): string {
  const p = algorithm.params
  const parts: string[] = []
  if (typeof p.total_duration === 'number') parts.push(`持续 ${p.total_duration} 秒`)
  if (typeof p.slices === 'number') parts.push(`分 ${p.slices} 次`)
  if (typeof p.participation_rate === 'number') parts.push(`参与率 ${Number((p.participation_rate * 100).toFixed(2))}%`)
  if (typeof p.max_wait_seconds === 'number') parts.push(`单次等待 ${p.max_wait_seconds} 秒`)
  if (p.price_strategy === 'PASSIVE') parts.push('被动挂单')
  if (p.price_strategy === 'ACTIVE') parts.push('主动报价')
  return parts.join(' · ')
}

export function algorithmScheduleSummary(algorithm: AlgorithmRef | null, count: number): string {
  return `${describeAlgorithmRef(algorithm)}${count ? ` · ${count} 个时段` : ''}`
}
