import { moveDay, parseDay } from '@/components/ui/datePickerModel'

export interface DateRange { from: string; to: string }

export function rangeDays(value: DateRange): number | null {
  const first = parseDay(value.from), last = parseDay(value.to)
  return first != null && last != null && last >= first ? (last - first) / 86400000 + 1 : null
}

/** 首次选起点；再次点更早的日期重选起点，结束日期包含第 maxDays 天。 */
export function pickRange(value: DateRange, anchor: string | null, day: string, maxDays: number): { value: DateRange; anchor: string | null } {
  if (!anchor || day < anchor) return { value: { from: day, to: '' }, anchor: day }
  if (day > moveDay(anchor, maxDays - 1)) return { value, anchor }
  return { value: { from: anchor, to: day }, anchor: null }
}

/** 快捷范围使用调用方提供的北京时间今天，不受浏览器时区影响。 */
export function presetRange(today: string, days: number, offset = 0): DateRange {
  const to = moveDay(today, offset)
  return { from: moveDay(to, 1 - days), to }
}
