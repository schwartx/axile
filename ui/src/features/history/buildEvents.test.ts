import { describe, it, expect } from 'bun:test'

import { buildEvents } from './derive'
import type { ScheduleSkipActivity } from '@/lib/api/accounts'
import type { ExecuteRecord, PortfolioAccountRecord } from '@/types/api'

/** 造一条执行记录。 */
function rec(id: number, ok: boolean, at: string, error?: string): ExecuteRecord {
  return {
    account_id: 1,
    id,
    execution_id: `exec-${id}`,
    input: {},
    result: { status: ok ? 'SUCCEEDED' : 'FAILED', error: error },
    is_success: ok ? 1 : 0,
    created_at: at,
  } as ExecuteRecord
}

/** 造一条绑定记录。 */
function bind(portfolioId: number | null, at: string): PortfolioAccountRecord {
  return { portfolio_id: portfolioId, created_at: at } as PortfolioAccountRecord
}

function skip(id: number, at: string): ScheduleSkipActivity {
  return {
    kind: 'schedule_skip',
    occurred_at: at,
    id,
    channel: 'ctp',
    reason_code: 'CALENDAR.CLOSED',
    calendar_day: at.slice(0, 10),
    calendar_id: 'china',
    calendar_label: '中国交易日历',
  }
}

describe('buildEvents', () => {
  it('失败逐条展开、带 executionId 与原因', () => {
    const events = buildEvents(
      [bind(2, '2026-07-14T00:16:00')],
      [
        rec(1, false, '2026-07-14T00:35:00', '算法参数非法'),
        rec(2, true, '2026-07-14T00:40:00'),
      ],
    )
    const fails = events.filter((e) => e.kind === 'fail')
    expect(fails).toHaveLength(1)
    expect(fails[0]?.executionId).toBe('exec-1')
    expect(fails[0]?.text).toContain('算法参数非法')
  })

  it('无原因时回退到「执行失败」', () => {
    const events = buildEvents([], [rec(9, false, '2026-07-14T01:00:00')])
    expect(events[0]?.text).toBe('执行失败')
    expect(events[0]?.executionId).toBe('exec-9')
  })

  it('超过上限的失败折叠成一条不可点的汇总', () => {
    const many = Array.from({ length: 11 }, (_, i) =>
      rec(i, false, `2026-07-14T0${i % 10}:00:00`),
    )
    const events = buildEvents([], many)
    const fails = events.filter((e) => e.kind === 'fail')
    // 8 条逐条 + 1 条折叠汇总
    expect(fails).toHaveLength(9)
    const summary = fails.find((e) => e.executionId == null)
    expect(summary?.text).toContain('3 次失败')
  })

  it('无失败时不产生失败事件', () => {
    const events = buildEvents([bind(2, '2026-07-14T00:16:00')], [rec(1, true, '2026-07-14T00:20:00')])
    expect(events.every((e) => e.kind !== 'fail')).toBe(true)
  })

  it('连续休市折叠为中性跳过事件', () => {
    const events = buildEvents(
      [],
      [],
      [skip(2, '2026-07-14T09:31:00+08:00'), skip(1, '2026-07-14T09:30:00+08:00')],
    )
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      kind: 'skip',
      tag: '跳过',
      text: '连续 2 次排程因休市跳过',
      executionId: null,
    })
  })

  it('不可用放行不会凭空产生跳过事件', () => {
    const events = buildEvents([], [rec(1, true, '2026-07-14T09:30:00+08:00')], [])
    expect(events.every((event) => event.kind !== 'skip')).toBe(true)
  })
})
