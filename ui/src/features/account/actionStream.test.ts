import { describe, it, expect } from 'bun:test'

import { buildSymbolActionStream } from './actionStream'
import type { ExecutionEvent } from '@/types/api'

/** 造一条最小可用的执行事件。 */
function ev(patch: Partial<ExecutionEvent>): ExecutionEvent {
  return {
    id: null,
    execution_id: 'e',
    account_id: 1,
    channel: 'PAPER',
    algorithm: 'single-maker',
    event_type: 'order_submitted',
    status: 'INFO',
    reason_code: '',
    reason_family: 'EXECUTION_STRATEGY',
    symbol: 'rb2610',
    order_id: null,
    client_order_id: null,
    ts_local_created: '2026-07-14T23:17:08',
    seq: 0,
    details: {},
    ...patch,
  } as ExecutionEvent
}

describe('buildSymbolActionStream', () => {
  it('直接提交的市价单显示订单类型，不显示零价', () => {
    const lines = buildSymbolActionStream([
      ev({ details: { order: { direction: 'BUY', order_type: 'MARKET', volume: 2, price: 0 } } }),
    ], 'rb2610')
    expect(lines[0]?.text).toBe('市价下单 买 2')
  })
  it.each(['部分成交', '待成交', '已报', 'CLOSED', 'FILLED', 'UNKNOWN'])('订单状态精确回放，不把 %s 判作全部成交', status => {
    const lines = buildSymbolActionStream([ev({ event_type: 'order_terminal', details: { order: { terminal_status: status, volume: 2, filled_volume: 1 } } })], 'rb2610')
    expect(lines[0].text).toContain(status)
    expect(lines[0].good).toBe(false)
  })
  it('决策置顶，即便其时间戳晚于成交（补发乱序已修）', () => {
    const lines = buildSymbolActionStream(
      [
        ev({
          event_type: 'order_terminal',
          seq: 5,
          status: 'SUCCESS',
          order_id: 'o1',
          ts_local_created: '2026-07-14T23:17:16',
          details: { order: { direction: 'OrderDirection.BUY', terminal_status: '已成交', volume: 0.0756, filled_volume: 0.0756, avg_price: 62000 } },
        }),
        // 决策补发在末尾，时间戳最晚：
        ev({
          event_type: 'symbol_decision_made',
          seq: 9,
          ts_local_created: '2026-07-14T23:17:20',
          details: { decision: { algorithm: 'single-maker', target_volume: 0.0756, orders_count: 1, status: 'SUCCEEDED' } },
        }),
        ev({
          event_type: 'order_submitted',
          seq: 2,
          order_id: 'o1',
          ts_local_created: '2026-07-14T23:17:08',
          details: { order: { direction: 'OrderDirection.BUY', volume: 0.0756, price: 61990 } },
        }),
      ],
      'rb2610',
    )

    expect(lines.map((l) => l.text)).toEqual([
      '决策 · 目标仓 0.0756 · single-maker · 1 单 · 调仓完成',
      '挂单 买 0.0756 @61,990',
      '成交 买 0.0756/0.0756 @62,000',
    ])
    expect(lines.every((l) => l.symbol === null)).toBe(true)
    // 三态：决策/挂单为中性过程步，成交为 accent 蓝正向确认，均非断点
    expect(lines.map((l) => [l.good, l.broken])).toEqual([
      [false, false],
      [false, false],
      [true, false],
    ])
  })

  it('追价成流，且抑制被追价撤掉的旧单终态', () => {
    const lines = buildSymbolActionStream(
      [
        ev({ event_type: 'order_submitted', seq: 1, order_id: 'o1', ts_local_created: '2026-07-14T23:17:08', details: { order: { direction: 'OrderDirection.BUY', volume: 1, price: 100 } } }),
        ev({ event_type: 'order_submitted', seq: 2, order_id: 'o2', reason_code: 'COMMON.ORDER_CHASE', ts_local_created: '2026-07-14T23:17:13', details: { chase: { index: 1, max: 5, from_price: 100, to_price: 101, prev_order_id: 'o1' } } }),
        // o1 被追价撤掉的终态——应被抑制：
        ev({ event_type: 'order_terminal', seq: 3, status: 'SUCCESS', order_id: 'o1', ts_local_created: '2026-07-14T23:17:14', details: { order: { direction: 'OrderDirection.BUY', terminal_status: '已撤销', volume: 1, filled_volume: 0 } } }),
        ev({ event_type: 'order_terminal', seq: 4, status: 'SUCCESS', order_id: 'o2', ts_local_created: '2026-07-14T23:17:20', details: { order: { direction: 'OrderDirection.BUY', terminal_status: '已成交', volume: 1, filled_volume: 1, avg_price: 101 } } }),
      ],
      'rb2610',
    )

    expect(lines.map((l) => l.text)).toEqual([
      '挂单 买 1 @100',
      '追价 → @101（第 1/5 次）',
      '成交 买 1/1 @101',
    ])
  })

  it('只取目标品种；跳过记为断点', () => {
    const events = [
      ev({ event_type: 'symbol_skipped', symbol: 'ag2612', seq: 1, reason_code: 'COMMON.SYMBOL_SKIPPED' }),
      ev({ event_type: 'symbol_decision_made', symbol: 'rb2610', seq: 2, details: { decision: { target_volume: 0.5 } } }),
    ]
    const ag = buildSymbolActionStream(events, 'ag2612')
    expect(ag.map((l) => [l.text, l.broken])).toEqual([['跳过 · 无需下单', true]])
    expect(buildSymbolActionStream(events, 'rb2610').length).toBe(1)
  })

  it('已知和未知原因码都不暴露内部枚举', () => {
    const known = buildSymbolActionStream(
      [ev({ event_type: 'symbol_skipped', symbol: 'ag2612', reason_code: 'COMMON.SUB_MIN_NOTIONAL' })],
      'ag2612',
    )
    const unknown = buildSymbolActionStream(
      [ev({ event_type: 'symbol_skipped', symbol: 'ag2612', reason_code: 'PLUGIN.UNKNOWN_REASON' })],
      'ag2612',
    )
    expect(known[0]?.text).toBe('跳过 · 未达到最小下单金额')
    expect(unknown[0]?.text).toBe('跳过 · 未执行')
  })

  it('无该品种事件 → 空流', () => {
    expect(buildSymbolActionStream([], 'rb2610')).toEqual([])
  })

  it('数量标签为空时只显示数量，并保留价格单位', () => {
    const lines = buildSymbolActionStream(
      [
        ev({
          event_type: 'order_submitted',
          details: { order: { direction: 'OrderDirection.BUY', volume: 0.001234, price: 62000 } },
        }),
      ],
      'rb2610',
      { quantityLabel: '', quantityMaxDecimals: 3, priceLabel: 'CNY' },
    )

    expect(lines[0]?.text).toBe('挂单 买 0.001 @62,000 元')
  })
})

it('新决策未到位时不复述旧失败摘要', () => {
  const lines = buildSymbolActionStream([ev({
    event_type: 'symbol_decision_made', status: 'ERROR',
    details: { decision: { status: 'PARTIAL', target_volume: 3 }, debug: { error: '持仓调整失败' } },
  })], 'rb2610')
  expect(lines[0].text).toContain('执行未全部完成')
  expect(lines[0].text).not.toContain('失败')
})

describe('平仓规则审计', () => {
  it('解释实际费用、人工兜底及分腿，不把零手续费省略', () => {
    const lines = buildSymbolActionStream([ev({ event_type: 'futures_close_planned', details: {
      futures_close_decision: { source: 'fee', priority: 'today_first', yesterday_cost: 10, today_cost: 0,
        preset_version: 'v1', legs: [{ volume: 1, offset_flag: 'close_today' }, { volume: 2, offset_flag: 'close_yesterday' }] },
    } })], 'rb2610')
    expect(lines[0].text).toContain('低手续费优先')
    expect(lines[0].text).toContain('平今 0.00')
    expect(lines[0].text).toContain('平今 1 手 → 平昨 2 手')
    expect(lines[0].text).toContain('预设 v1')
  })
})
