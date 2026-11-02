import { describe, expect, test } from 'bun:test'
import type { SchedulePreview } from '@/lib/api/accounts'
import {
  appendSchedulePreview,
  isClosedPreviewDay,
  PREVIEW_MAX_ITEMS,
  PREVIEW_MIN_ITEMS,
  previewLimitForHeight,
  previewRequestLimit,
  schedulePreviewItemPresentation,
  schedulePreviewNextCursor,
  schedulePreviewRows,
} from '@/features/setup/previewTimeline'
import { executionReasonText } from '@/features/account/executionReason'

function preview(times: string[], nextCursor: string | null, hasMore: boolean): SchedulePreview {
  return {
    timezone: 'Asia/Shanghai',
    evaluated_at: '2026-08-26T15:00:00+08:00',
    calendar: {
      requirement: 'required',
      availability: 'available',
      unavailable_reason: null,
      calendar_id: 'china',
      label: '中国交易日历',
      coverage_start: '2003-01-01',
      coverage_end: '2026-12-31',
    },
    items: times.map((scheduledAt) => ({
      scheduled_at: scheduledAt,
      calendar_day: scheduledAt.slice(0, 10),
      calendar_status: 'available_open',
      action: 'execute',
      unavailable_reason: null,
      calendar_id: 'china',
      label: '中国交易日历',
      reason_code: null,
    })),
    next_cursor: nextCursor,
    has_more: hasMore,
  }
}

describe('previewLimitForHeight', () => {
  test('按可视高度计算并夹在后端安全范围内', () => {
    expect(previewLimitForHeight(0)).toBe(PREVIEW_MIN_ITEMS)
    expect(previewLimitForHeight(260)).toBe(13)
    expect(previewLimitForHeight(100_000)).toBe(PREVIEW_MAX_ITEMS)
  })
})

test('请求条数按补发轮次换算，遵守后端单页上限', () => {
  expect(previewRequestLimit(5, 0)).toBe(5)
  expect(previewRequestLimit(5, 2)).toBe(15)
  expect(previewRequestLimit(50, 4)).toBe(PREVIEW_MAX_ITEMS)
})

describe('schedulePreviewRows', () => {
  test('跨页连续休市合并为日期区间，保留有效计划且不修改原始数据', () => {
    const closed = preview(['2026-08-29T09:00:00+08:00', '2026-08-29T09:15:00+08:00'], '2026-08-29T09:15:00+08:00', true)
    closed.items = closed.items.map((item) => ({ ...item, calendar_status: 'available_closed', action: 'skip', reason_code: 'CALENDAR.CLOSED' }))
    const next = preview(['2026-08-29T09:30:00+08:00', '2026-08-30T09:00:00+08:00', '2026-08-31T09:00:00+08:00'], null, false)
    next.items = next.items.map((item) => item.calendar_day === '2026-08-31' ? item : {
      ...item, calendar_status: 'available_closed', action: 'skip', reason_code: 'CALENDAR.CLOSED',
    })
    const rows = schedulePreviewRows(appendSchedulePreview(closed, next, closed.next_cursor!).items)
    expect(rows.map((item) => item.calendar_day)).toEqual(['2026-08-29', '2026-08-31'])
    expect(rows[0]!.closed_until).toBe('2026-08-30')
    expect(rows.map(isClosedPreviewDay)).toEqual([true, false])
    expect(closed.items[0]).not.toHaveProperty('closed_until')
    expect(schedulePreviewRows(closed.items)[0]).not.toHaveProperty('closed_until')
  })

  test.each([
    ['2026-10-31', '2026-11-01'],
    ['2026-12-31', '2027-01-01'],
  ])('连续休市跨月或跨年仍合并：%s–%s', (start, end) => {
    const points = preview([`${start}T09:15:00+08:00`, `${end}T09:15:00+08:00`], null, false)
    points.items = points.items.map((item) => ({ ...item, calendar_status: 'available_closed', action: 'skip', reason_code: 'CALENDAR.CLOSED' }))
    const rows = schedulePreviewRows(points.items)
    expect(rows).toHaveLength(1)
    expect(rows[0]!.closed_until).toBe(end)
  })

  test('不跨过未加载日期、交易日或日历不可用日期合并', () => {
    const points = preview(['2026-11-01', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-07', '2026-11-08'].map((day) => `${day}T09:15:00+08:00`), null, false)
    points.items = points.items.map((item, index) => {
      if (index === 2) return item
      return { ...item, calendar_status: index === 4 ? 'unavailable' : 'available_closed', action: 'skip', reason_code: index === 4 ? null : 'CALENDAR.CLOSED' }
    })
    const rows = schedulePreviewRows(points.items)
    expect(rows.map((row) => row.calendar_day)).toEqual(['2026-11-01', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-07'])
    expect(rows.map((row) => row.closed_until)).toEqual([undefined, undefined, undefined, undefined, undefined, '2026-11-08'])
  })

  test('日内休息、无对应夜盘和日历不可用仍显示各自时刻', () => {
    const points = preview(['2026-08-28T12:00:00+08:00', '2026-08-28T12:15:00+08:00', '2026-08-28T21:00:00+08:00', '2026-08-28T21:15:00+08:00'], null, false)
    points.items = points.items.map((item, index) => ({
      ...item,
      calendar_status: index === 3 ? 'unavailable' : 'available_closed',
      action: 'skip',
      reason_code: index < 2 ? 'CALENDAR.SESSION_CLOSED' : 'CALENDAR.NO_NIGHT_SESSION',
    }))
    expect(schedulePreviewRows(points.items)).toEqual(points.items)
    expect(points.items.every((item) => !isClosedPreviewDay(item))).toBe(true)
  })

  test('同一天的多轮独立展示，每轮补发合并，跨页续取不增加重复行', () => {
    const firstBase = '2026-08-27T14:00:00+08:00'
    const secondBase = '2026-08-27T14:15:00+08:00'
    const first = preview([firstBase, '2026-08-27T14:01:00+08:00'], '2026-08-27T14:01:00+08:00', true)
    first.items = first.items.map((item, index) => ({ ...item, base_scheduled_at: firstBase, index, effective_count: 2 }))
    const next = preview(['2026-08-27T14:02:00+08:00', secondBase], secondBase, true)
    next.items[0] = { ...next.items[0]!, base_scheduled_at: firstBase, index: 2, effective_count: 2 }
    next.items[1] = { ...next.items[1]!, base_scheduled_at: secondBase, index: 0, effective_count: 2 }

    const merged = appendSchedulePreview(first, next, first.next_cursor!)
    expect(schedulePreviewRows(merged.items).map((item) => item.scheduled_at)).toEqual([firstBase, secondBase])
  })

  test('旧 cron 的相邻触发点不推断为补发', () => {
    const legacy = preview(['2026-08-27T14:00:00+08:00', '2026-08-27T14:01:00+08:00'], null, false)
    expect(schedulePreviewRows(legacy.items)).toEqual(legacy.items)
  })
})

test('整日休市续取从当天结束开始，日内休息及日历不可用按原游标继续', () => {
  const first = preview(['2026-08-29T09:00:00+08:00'], '2026-08-29T09:00:00+08:00', true)
  const base = first.items[0]!
  first.items[0] = { ...base, calendar_status: 'available_closed', action: 'skip', reason_code: 'CALENDAR.CLOSED' }
  expect(schedulePreviewNextCursor(first)).toBe('2026-08-29T23:59:59.999+08:00')
  first.items[0] = { ...first.items[0], reason_code: 'CALENDAR.SESSION_CLOSED' }
  expect(schedulePreviewNextCursor(first)).toBe(first.next_cursor)
  first.items[0] = { ...base, calendar_status: 'unavailable' }
  expect(schedulePreviewNextCursor(first)).toBe(first.next_cursor)
})

describe('appendSchedulePreview', () => {
  test('边界去重并续接推进后的游标', () => {
    const first = preview(['2026-08-27T15:00:00+08:00', '2026-08-27T15:01:00+08:00'], '2026-08-27T15:01:00+08:00', true)
    const next = preview(['2026-08-27T15:01:00+08:00', '2026-08-27T15:02:00+08:00'], '2026-08-27T15:02:00+08:00', true)
    const merged = appendSchedulePreview(first, next, '2026-08-27T15:01:00+08:00')
    expect(merged.items.map((item) => item.scheduled_at)).toEqual([
      '2026-08-27T15:00:00+08:00',
      '2026-08-27T15:01:00+08:00',
      '2026-08-27T15:02:00+08:00',
    ])
    expect(merged.has_more).toBe(true)
  })

  test('游标未推进或没有新条目时停止自动续取', () => {
    const cursor = '2026-08-27T15:01:00+08:00'
    const first = preview([cursor], cursor, true)
    const stalled = preview([cursor], cursor, true)
    expect(appendSchedulePreview(first, stalled, cursor).has_more).toBe(false)
  })
})

describe('schedulePreviewItemPresentation', () => {
  test('区分执行、跳过与日历降级的文案和颜色语义', () => {
    const base = preview(['2026-08-27T15:00:00+08:00'], null, false).items[0]!

    expect(schedulePreviewItemPresentation(base, executionReasonText)).toEqual({
      text: '交易日，执行',
      tone: 'default',
    })
    expect(schedulePreviewItemPresentation({
      ...base,
      calendar_status: 'available_closed',
      action: 'skip',
      reason_code: 'CALENDAR.NO_NIGHT_SESSION',
    }, executionReasonText)).toEqual({
      text: '无对应夜盘',
      tone: 'muted',
    })
    expect(schedulePreviewItemPresentation({
      ...base,
      calendar_status: 'unavailable',
    }, executionReasonText)).toEqual({
      text: '日历不可用，跳过执行',
      tone: 'warning',
    })
  })

  test('一次展示有效补发次数与间隔，不依赖当前页包含全部步骤', () => {
    const base = {
      ...preview(['2026-08-27T14:55:00+08:00'], null, false).items[0]!,
      base_scheduled_at: '2026-08-27T14:55:00+08:00',
      index: 0,
      effective_count: 2,
    }
    expect(schedulePreviewItemPresentation(base, executionReasonText, 1)).toEqual({
      text: '补 2 次 · 隔 1 分',
      tone: 'default',
    })
    expect(schedulePreviewItemPresentation({ ...base, effective_count: 1 }, executionReasonText, 3).text).toBe('补 1 次 · 隔 3 分')
    expect(schedulePreviewItemPresentation({ ...base, effective_count: 0 }, executionReasonText, 1).text).toBe('补 0 次')
    expect(schedulePreviewItemPresentation({
      ...base,
      calendar_status: 'available_closed',
      action: 'skip',
      reason_code: 'CALENDAR.CLOSED',
    }, executionReasonText, 1)).toEqual({ text: '休市', tone: 'muted' })
  })

  test('从本轮中途预览时保留下一次补发的时间，仅显示尚未到点的次数', () => {
    const remaining = preview(['2026-08-27T14:57:00+08:00', '2026-08-27T14:58:00+08:00'], null, false)
    remaining.items = remaining.items.map((item, index) => ({
      ...item,
      base_scheduled_at: '2026-08-27T14:55:00+08:00',
      index: index + 2,
      effective_count: 3,
    }))
    const rows = schedulePreviewRows(remaining.items)
    expect(rows).toHaveLength(1)
    expect(rows[0]!.scheduled_at).toBe('2026-08-27T14:57:00+08:00')
    expect(schedulePreviewItemPresentation(rows[0]!, executionReasonText, 1).text).toBe('待补 2 次 · 隔 1 分')
  })
})
