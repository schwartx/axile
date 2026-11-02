import type { CSSProperties, ReactNode } from 'react'

import { formatBeijingDate, formatBeijingTimestamp, formatPlannedAt, formatPlannedDate } from '@/lib/scheduleTime'

export type ScheduleTimeRowTone = 'default' | 'muted' | 'warning'
export type ScheduleTimeRowSize = 'sm' | 'md'

export interface ScheduleTimeRowProps {
  scheduledAt: string
  dateOnly?: boolean
  dateEnd?: string
  trailing: ReactNode
  now?: number
  tone?: ScheduleTimeRowTone
  size?: ScheduleTimeRowSize
  stacked?: boolean
  className?: string
  style?: CSSProperties
}

const TONE_CLASS: Record<ScheduleTimeRowTone, string> = {
  default: 'text-ink-2',
  muted: 'text-ink-3',
  warning: 'text-warn',
}

const SIZE_CLASS: Record<ScheduleTimeRowSize, string> = {
  sm: 'text-[14px]',
  md: 'text-[15px]',
}

/** 通用排程行：自然时间在左，场景化状态或相对时间在右。 */
export function ScheduleTimeRow({
  scheduledAt,
  dateOnly = false,
  dateEnd,
  trailing,
  now = Date.now(),
  tone = 'default',
  size = 'sm',
  stacked = false,
  className = '',
  style,
}: ScheduleTimeRowProps) {
  const dateTime = dateOnly ? formatBeijingDate(scheduledAt) : scheduledAt
  const rangeEnd = dateOnly ? dateEnd : undefined
  const exactTime = dateOnly ? `${dateTime}${rangeEnd ? `–${rangeEnd}` : ''}（北京时间）` : formatBeijingTimestamp(scheduledAt)
  return (
    <div
      role="listitem"
      className={`grid min-w-0 items-baseline ${stacked ? 'grid-cols-1 gap-1 py-1' : rangeEnd ? 'grid-cols-[minmax(0,1fr)_auto] gap-3' : 'grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3'} ${SIZE_CLASS[size]} ${className}`}
      style={style}
    >
      <time
        dateTime={dateTime}
        title={exactTime}
        className={`num min-w-0 font-medium text-ink-1 ${stacked || rangeEnd ? '' : 'truncate'}`}
      >
        {dateOnly ? formatPlannedDate(scheduledAt, now) : formatPlannedAt(scheduledAt, now)}
        {rangeEnd && `–${formatPlannedDate(rangeEnd, now)}`}
      </time>
      <span title={typeof trailing === 'string' ? trailing : undefined} className={`min-w-0 ${stacked ? 'break-words text-left' : 'truncate text-right'} ${TONE_CLASS[tone]}`}>
        {trailing}
      </span>
    </div>
  )
}
