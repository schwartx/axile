import { describe, expect, it } from 'bun:test'

import { formatBeijingDate, formatBeijingTimestamp, formatObservedAt, formatPlannedAt, formatPlannedDate, formatRecentExecution, formatTimeUntil } from '@/lib/scheduleTime'

const NOW = Date.parse('2026-08-24T09:30:00+08:00')

describe('formatRecentExecution', () => {
  it('formats just now, minutes, and hours', () => {
    expect(formatRecentExecution('2026-08-24T09:29:31+08:00', NOW)).toBe('刚刚')
    expect(formatRecentExecution('2026-08-24T09:12:00+08:00', NOW)).toBe('18 分钟前')
    expect(formatRecentExecution('2026-08-24T07:00:00+08:00', NOW)).toBe('2 小时前')
  })

  it('formats yesterday and older dates across years', () => {
    expect(formatRecentExecution('2026-08-23T08:00:00+08:00', NOW)).toBe('昨天 08:00')
    expect(formatRecentExecution('2025-12-31T23:00:00+08:00', NOW)).toBe('2025 年 12 月 31 日 23:00')
  })
})

describe('formatObservedAt', () => {
  it('按北京时间判断今天和昨天', () => {
    expect(formatObservedAt('2026-08-23T16:00:00.000000Z', NOW)).toBe('今天 00:00')
    expect(formatObservedAt('2026-08-23T15:59:59.000000Z', NOW)).toBe('昨天 23:59')
  })

  it('显示历史观测日期，跨年补年份', () => {
    const observation = '2026-09-28T06:12:56.000000Z'
    expect(formatObservedAt(observation, Date.parse('2026-11-11T00:00:00Z'))).toBe('9 月 28 日 14:12')
    expect(formatObservedAt(observation, Date.parse('2027-01-01T00:00:00Z'))).toBe('2026 年 9 月 28 日 14:12')
    expect(formatBeijingTimestamp(observation)).toBe('2026-09-28 14:12:56（北京时间）')
    expect(formatObservedAt('invalid', NOW)).toBe('—')
  })
})

describe('future schedule formatting', () => {
  it('休市日期按北京时间计算自然日，跨年保留年份', () => {
    expect(formatPlannedDate('2026-08-24T15:59:00Z', NOW)).toBe('今天')
    expect(formatPlannedDate('2026-08-24T16:00:00Z', NOW)).toBe('明天')
    expect(formatBeijingDate('2026-08-24T16:00:00Z')).toBe('2026-08-25')
    expect(formatPlannedDate('2027-01-02T09:00:00+08:00', NOW)).toBe('2027 年 1 月 2 日')
  })

  it('pairs natural dates with relative time', () => {
    expect(formatPlannedAt('2026-08-24T09:48:00+08:00', NOW)).toBe('今天 09:48')
    expect(formatTimeUntil('2026-08-24T09:48:00+08:00', NOW)).toBe('18 分钟后')
    expect(formatPlannedAt('2026-08-25T15:00:00+08:00', NOW)).toBe('明天 15:00')
    expect(formatTimeUntil('2026-08-24T11:01:00+08:00', NOW)).toBe('2 小时后')
  })

  it('shows the year when a plan crosses into another year', () => {
    const yearEnd = Date.parse('2026-12-31T23:30:00+08:00')
    expect(formatPlannedAt('2027-01-01T10:00:00+08:00', yearEnd)).toBe('明天 10:00')
    expect(formatPlannedAt('2027-01-02T10:00:00+08:00', yearEnd)).toBe('2027 年 1 月 2 日 10:00')
  })

  it('provides a precise Beijing timestamp for hover text', () => {
    expect(formatBeijingTimestamp('2026-08-24T01:48:00Z')).toBe('2026-08-24 09:48:00（北京时间）')
  })
})
