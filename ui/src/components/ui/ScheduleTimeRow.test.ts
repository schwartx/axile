import { describe, expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { ScheduleTimeRow } from './ScheduleTimeRow'

const NOW = Date.parse('2026-08-27T09:00:00+08:00')

describe('ScheduleTimeRow', () => {
  it('连续休市显示日期区间，给日期留出整行宽度', () => {
    const html = renderToStaticMarkup(createElement(ScheduleTimeRow, {
      scheduledAt: '2026-11-07T09:15:00+08:00',
      dateOnly: true,
      dateEnd: '2026-11-08',
      trailing: '休市',
      now: NOW,
    }))
    expect(html).toContain('11 月 7 日–11 月 8 日')
    expect(html).toContain('2026-11-07–2026-11-08（北京时间）')
    expect(html).toContain('grid-cols-[minmax(0,1fr)_auto]')
    expect(html).toContain('休市')
    expect(html).not.toContain('09:15')
  })

  it('两行预览完整显示阶段和补发说明，不截断时间或说明', () => {
    const html = renderToStaticMarkup(createElement(ScheduleTimeRow, {
      scheduledAt: '2026-09-01T09:15:00+08:00',
      trailing: '早盘集合竞价 · 可撤单阶段 · 补发 2 次',
      now: NOW,
      stacked: true,
    }))
    expect(html).toContain('grid-cols-1')
    expect(html).toContain('早盘集合竞价 · 可撤单阶段 · 补发 2 次')
    expect(html).toContain('break-words text-left')
    expect(html).not.toContain('truncate')
  })

  it('休市行仅显示北京时间日期，日期属性与悬浮提示也省略时刻', () => {
    const html = renderToStaticMarkup(createElement(ScheduleTimeRow, {
      scheduledAt: '2026-08-27T16:30:00Z',
      dateOnly: true,
      trailing: '休市',
      now: NOW,
      tone: 'muted',
    }))
    expect(html).toContain('dateTime="2026-08-28"')
    expect(html).toContain('2026-08-28（北京时间）')
    expect(html).toContain('>明天</time>')
    expect(html).toContain('休市')
    expect(html).not.toContain('16:30')
    expect(html).not.toContain('00:30')
  })

  it('renders a natural date with an exact Beijing timestamp', () => {
    const html = renderToStaticMarkup(createElement(ScheduleTimeRow, {
      scheduledAt: '2026-08-27T15:00:00+08:00',
      trailing: '6 小时后',
      now: NOW,
      tone: 'muted',
      size: 'md',
    }))

    expect(html).toContain('今天 15:00')
    expect(html).toContain('6 小时后')
    expect(html).toContain('2026-08-27 15:00:00（北京时间）')
    expect(html).toContain('text-ink-3')
    expect(html).toContain('text-[15px]')
  })

  it('uses warning color without changing the schedule time color', () => {
    const html = renderToStaticMarkup(createElement(ScheduleTimeRow, {
      scheduledAt: '2026-08-28T15:00:00+08:00',
      trailing: '日历不可用，按排程执行',
      now: NOW,
      tone: 'warning',
    }))

    expect(html).toContain('明天 15:00')
    expect(html).toContain('text-warn')
    expect(html).toContain('font-medium text-ink-1')
  })
})
