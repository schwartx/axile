import { describeAlgorithmRef, validateAlgorithmRef } from '@/features/setup/algorithms'
import type { AlgorithmRef, AlgorithmTimeRule } from '@/types/api'

export type AlgorithmRuleDraft = AlgorithmTimeRule & { key: string; autoEnd?: boolean }

/** 自动补齐的结束时间跟随开始时间；用户手动选择或保存后保留已设置的结束时间。 */
export function algorithmStartChange(rule: AlgorithmRuleDraft, start: string): Partial<AlgorithmRuleDraft> {
  if (rule.end && !rule.autoEnd) return { start }
  const [hour, minute] = start.split(':').map(Number)
  const next = (hour * 60 + minute + 1) % (24 * 60)
  const end = `${String(Math.floor(next / 60)).padStart(2, '0')}:${String(next % 60).padStart(2, '0')}`
  return { start, end, autoEnd: true }
}

export function algorithmScheduleDraft(rules: AlgorithmTimeRule[], previous: AlgorithmRuleDraft[] = []): AlgorithmRuleDraft[] {
  return rules.toSorted((a, b) => a.start.localeCompare(b.start))
    .map(rule => ({ ...structuredClone(rule), key: previous.find(item => item.start === rule.start && item.end === rule.end)?.key ?? crypto.randomUUID() }))
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

export function algorithmScheduleSummary(algorithm: AlgorithmRef | null, count: number, products = 0): string {
  return `${describeAlgorithmRef(algorithm)}${count ? ` · ${count} 个时段` : ''}${products ? ` · ${products} 个品种` : ''}`
}
