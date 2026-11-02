import type { SchedulePreview } from '@/lib/api/accounts'
import type { ScheduleTimeRowTone } from '@/components/ui/ScheduleTimeRow'

export const PREVIEW_ROW_PITCH = 26
export const PREVIEW_MIN_ITEMS = 5
export const PREVIEW_MAX_ITEMS = 100
export const PREVIEW_PREFETCH_ROWS = 3

export interface SchedulePreviewItemPresentation {
  text: string
  tone: ScheduleTimeRowTone
}

/** 整日休市可合并日期；日内市场缝和无对应夜盘仍保留具体时刻。 */
export function isClosedPreviewDay(item: SchedulePreview['items'][number]): boolean {
  return item.calendar_status === 'available_closed'
    && (item.reason_code == null || item.reason_code === 'CALENDAR.CLOSED')
}

export function schedulePreviewRowKey(item: SchedulePreview['items'][number]): string {
  return isClosedPreviewDay(item)
    ? `closed:${item.calendar_day}`
    : `round:${item.base_scheduled_at ?? item.scheduled_at}`
}

export type SchedulePreviewRow = SchedulePreview['items'][number] & { closed_until?: string }

/** 连续整日休市合并为日期区间，补发合并为基础轮次，普通 cron 各自保留。 */
export function schedulePreviewRows(items: SchedulePreview['items']): SchedulePreviewRow[] {
  const seen = new Set<string>()
  const rows: SchedulePreviewRow[] = []
  for (const item of items) {
    const key = schedulePreviewRowKey(item)
    if (seen.has(key)) continue
    seen.add(key)
    const previous = rows.at(-1)
    if (previous && isClosedPreviewDay(previous) && isClosedPreviewDay(item)
      && previous.calendar_id === item.calendar_id
      && Date.parse(item.calendar_day) - Date.parse(previous.closed_until ?? previous.calendar_day) === 86_400_000) {
      previous.closed_until = item.calendar_day
    } else {
      rows.push({ ...item })
    }
  }
  return rows
}

/** 日期行已代表整日休市，续取从当天结束后开始，避免密集重复拉取。 */
export function schedulePreviewNextCursor(preview: SchedulePreview): string | null {
  const last = preview.items.at(-1)
  if (!preview.next_cursor || !last || !isClosedPreviewDay(last)) return preview.next_cursor
  const dayEnd = `${last.calendar_day}T23:59:59.999+08:00`
  return Date.parse(dayEnd) > Date.parse(preview.next_cursor) ? dayEnd : preview.next_cursor
}

/** 将服务端日历决策收口为排程行的人读结果与颜色语义。 */
export function schedulePreviewItemPresentation(
  item: SchedulePreview['items'][number],
  reasonText: (reasonCode: string | null | undefined, fallback: string) => string,
  intervalMinutes?: number,
): SchedulePreviewItemPresentation {
  if (item.calendar_status === 'available_closed') {
    return { text: reasonText(item.reason_code, '休市').replace(/，已跳过$/, ''), tone: 'muted' }
  }
  if (item.calendar_status === 'unavailable') return { text: '日历不可用，跳过执行', tone: 'warning' }
  if (item.action === 'execute' && item.base_scheduled_at && intervalMinutes != null) {
    const index = item.index ?? 0
    const count = Math.max(0, (item.effective_count ?? 0) - Math.max(0, index - 1))
    const label = index > 0 ? '待补' : '补'
    return { text: `${label} ${count} 次${count > 0 ? ` · 隔 ${intervalMinutes} 分` : ''}`, tone: 'default' }
  }
  if (item.calendar_status === 'available_open') return { text: '交易日，执行', tone: 'default' }
  return { text: '按排程执行', tone: 'default' }
}

/** 后端按触发点分页，按每轮最多的触发数换算所需条数。 */
export function previewRequestLimit(rows: number, supplementCount: number): number {
  return Math.min(PREVIEW_MAX_ITEMS, rows * (supplementCount + 1))
}

/** 按列表可视高度计算首屏及后续批次条数。 */
export function previewLimitForHeight(height: number): number {
  if (!Number.isFinite(height) || height <= 0) return PREVIEW_MIN_ITEMS
  return Math.min(
    PREVIEW_MAX_ITEMS,
    Math.max(
      PREVIEW_MIN_ITEMS,
      Math.ceil(height / PREVIEW_ROW_PITCH) + PREVIEW_PREFETCH_ROWS,
    ),
  )
}

/** 续接未来时间线；边界去重且在游标未推进时停止自动续取。 */
export function appendSchedulePreview(
  current: SchedulePreview,
  next: SchedulePreview,
  requestedAfter: string,
): SchedulePreview {
  const seen = new Set(current.items.map((item) => item.scheduled_at))
  const appended = next.items.filter((item) => !seen.has(item.scheduled_at))
  const cursorAdvanced = next.next_cursor != null && next.next_cursor !== requestedAfter
  return {
    ...current,
    evaluated_at: next.evaluated_at,
    items: [...current.items, ...appended],
    next_cursor: next.next_cursor,
    has_more: next.has_more && cursorAdvanced && appended.length > 0,
  }
}
