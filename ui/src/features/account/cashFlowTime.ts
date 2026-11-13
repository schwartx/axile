import { parseDay } from '@/components/ui/datePickerModel'
import { formatBeijingDate } from '@/lib/scheduleTime'

/** 分段草稿只有完整有效时才转换为北京时间，避免自动修正非法日期。 */
export function cashFlowTimestamp(date: string, time: string): string | null {
  if (parseDay(date) == null || date < '0001-01-01'
    || !/^(?:[01]?\d|2[0-3]):[0-5]?\d:[0-5]?\d$/.test(time)) return null
  return `${date}T${time.split(':').map(part => part.padStart(2, '0')).join(':')}+08:00`
}

/** 日期一旦完整即可限制未来日；当天时间完整后再与当前时刻比较。 */
export function cashFlowTimeError(date: string, time: string, now = Date.now()): string | null {
  if (parseDay(date) == null) return null
  const timestamp = cashFlowTimestamp(date, time)
  return date > formatBeijingDate(new Date(now).toISOString()) || (timestamp != null && Date.parse(timestamp) > now)
    ? '发生时间不能晚于当前时间'
    : null
}
