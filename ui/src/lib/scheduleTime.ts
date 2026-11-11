/** 排程时间的人性化北京时间展示。 */

const TIMEZONE = 'Asia/Shanghai'

interface DateParts {
  year: number
  month: number
  day: number
  hour: number
  minute: number
  second: number
}

const partsFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: TIMEZONE,
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

function dateParts(value: string | number | Date): DateParts | null {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return null
  const values = Object.fromEntries(
    partsFormatter
      .formatToParts(date)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, Number(part.value)]),
  )
  return values as unknown as DateParts
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function dayNumber(parts: DateParts): number {
  return Date.UTC(parts.year, parts.month - 1, parts.day) / 86_400_000
}

/** 最近一次执行：一天内用相对时间，更早用自然日期。 */
export function formatRecentExecution(iso: string, now = Date.now()): string {
  const timestamp = new Date(iso).getTime()
  if (!Number.isFinite(timestamp)) return '—'
  const elapsed = Math.max(0, now - timestamp)
  if (elapsed < 60_000) return '刚刚'
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)} 分钟前`
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)} 小时前`

  const target = dateParts(timestamp)
  const current = dateParts(now)
  if (!target || !current) return '—'
  const time = `${pad(target.hour)}:${pad(target.minute)}`
  const daysAgo = dayNumber(current) - dayNumber(target)
  if (daysAgo === 1) return `昨天 ${time}`
  const year = target.year === current.year ? '' : `${target.year} 年 `
  return `${year}${target.month} 月 ${target.day} 日 ${time}`
}

function plannedDate(target: DateParts, current: DateParts): string {
  const daysAfter = dayNumber(target) - dayNumber(current)
  if (daysAfter === 0) return '今天'
  if (daysAfter === 1) return '明天'
  const year = target.year === current.year ? '' : `${target.year} 年 `
  return `${year}${target.month} 月 ${target.day} 日`
}

/** 休市日的自然日期，省略没有执行意义的时刻。 */
export function formatPlannedDate(iso: string, now = Date.now()): string {
  const target = dateParts(iso)
  const current = dateParts(now)
  return target && current ? plannedDate(target, current) : '—'
}

/** 未来计划的自然日期，如「今天 10:00」「明天 15:00」。 */
export function formatPlannedAt(iso: string, now = Date.now()): string {
  const target = dateParts(iso)
  const current = dateParts(now)
  if (!target || !current) return '—'
  return `${plannedDate(target, current)} ${pad(target.hour)}:${pad(target.minute)}`
}

/** 已观测时间用北京时间的自然日和时分展示。 */
export function formatObservedAt(iso: string, now = Date.now()): string {
  const target = dateParts(iso)
  const current = dateParts(now)
  if (!target || !current) return '—'
  const time = `${pad(target.hour)}:${pad(target.minute)}`
  const daysAgo = dayNumber(current) - dayNumber(target)
  if (daysAgo === 0) return `今天 ${time}`
  if (daysAgo === 1) return `昨天 ${time}`
  const year = target.year === current.year ? '' : `${target.year} 年 `
  return `${year}${target.month} 月 ${target.day} 日 ${time}`
}

/** 北京时间日期，用作日期行的机器可读值。 */
export function formatBeijingDate(iso: string): string {
  const parts = dateParts(iso)
  return parts ? `${parts.year}-${pad(parts.month)}-${pad(parts.day)}` : iso
}

/** 未来计划距现在多久；轮询窗口内已到点的任务显示「即将执行」。 */
export function formatTimeUntil(iso: string, now = Date.now()): string {
  const timestamp = new Date(iso).getTime()
  if (!Number.isFinite(timestamp)) return '—'
  const remaining = timestamp - now
  if (remaining <= 0) return '即将执行'
  if (remaining < 3_600_000) return `${Math.max(1, Math.ceil(remaining / 60_000))} 分钟后`
  if (remaining < 86_400_000) return `${Math.ceil(remaining / 3_600_000)} 小时后`
  return `${Math.ceil(remaining / 86_400_000)} 天后`
}

/** 完整北京时间，用于悬浮确认精确时刻。 */
export function formatBeijingTimestamp(iso: string): string {
  const parts = dateParts(iso)
  if (!parts) return iso
  return `${parts.year}-${pad(parts.month)}-${pad(parts.day)} ${pad(parts.hour)}:${pad(parts.minute)}:${pad(parts.second)}（北京时间）`
}
