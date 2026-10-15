const DAY = 86400000

/** 使用 UTC 计算日历，避免浏览器时区与夏令时改变自然日。 */
export function parseDay(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const time = Date.parse(value + 'T00:00:00Z')
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value ? time : null
}

export function formatDay(time: number): string {
  return new Date(time).toISOString().slice(0, 10)
}

export function moveDay(value: string, days: number): string {
  return formatDay(parseDay(value)! + days * DAY)
}

export function moveMonth(value: string, months: number): string {
  const date = new Date(parseDay(value)!)
  const day = date.getUTCDate()
  date.setUTCDate(1)
  date.setUTCMonth(date.getUTCMonth() + months)
  const last = new Date(date)
  last.setUTCMonth(last.getUTCMonth() + 1)
  last.setUTCDate(0)
  date.setUTCDate(Math.min(day, last.getUTCDate()))
  return formatDay(date.getTime())
}

export function clampDay(value: string, min?: string, max?: string): string {
  if (min && parseDay(min) != null && value < min) return min
  if (max && parseDay(max) != null && value > max) return max
  return value
}

/** 固定六周、周一开头，翻月时日历尺寸保持不变。 */
export function monthDays(month: string): string[] {
  const first = month + '-01'
  const weekday = (new Date(parseDay(first)!).getUTCDay() + 6) % 7
  return Array.from({ length: 42 }, (_, index) => moveDay(first, index - weekday))
}

/** 不渲染月末之后整周的空白；双月日历可取两个月的最大值对齐。 */
export function monthWeeks(month: string): number {
  const days = monthDays(month)
  const lastIndex = days.findLastIndex(day => day.startsWith(month))
  return Math.ceil((lastIndex + 1) / 7)
}

export function calendarKeyDay(value: string, key: string): string | null {
  const weekday = (new Date(parseDay(value)!).getUTCDay() + 6) % 7
  switch (key) {
    case 'ArrowLeft': return moveDay(value, -1)
    case 'ArrowRight': return moveDay(value, 1)
    case 'ArrowUp': return moveDay(value, -7)
    case 'ArrowDown': return moveDay(value, 7)
    case 'Home': return moveDay(value, -weekday)
    case 'End': return moveDay(value, 6 - weekday)
    case 'PageUp': return moveMonth(value, -1)
    case 'PageDown': return moveMonth(value, 1)
    default: return null
  }
}
