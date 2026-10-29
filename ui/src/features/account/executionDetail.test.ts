import { describe, it, expect } from 'bun:test'

import { buildExecutionDetail, executionHeadline, formatOrderTradeCounts } from './executionDetail'

import type { ExecutionArtifact, ExecutionEvent, ExecutionStatus } from '@/types/api'

/** 造一条最小可用的执行事件。 */
function ev(patch: Partial<ExecutionEvent>): ExecutionEvent {
  return {
    event_type: 'execution_started',
    status: 'INFO',
    reason_code: '',
    reason_family: 'SYSTEM',
    symbol: null,
    ts_local_created: '2026-07-14T10:18:08',
    seq: 0,
    details: {},
    ...patch,
  } as ExecutionEvent
}

/** 造一个附件。 */
function art(type: string, content: Record<string, unknown>): ExecutionArtifact {
  return { id: null, execution_id: 'e', artifact_type: type, content, created_at: '' }
}

/** 一组「两只减仓、全部到位」的真实形态 fixture（对应 4c05）。 */
function baseFixture(): { events: ExecutionEvent[]; artifacts: ExecutionArtifact[]; summary: Record<string, unknown> } {
  const events: ExecutionEvent[] = [
    ev({ event_type: 'execution_started', seq: 1, details: { debug: { trigger_source: 'manual', execution_kind: 'rebalance' } } }),
    ev({ event_type: 'input_snapshotted', seq: 2, details: { debug: { strategy_count: 0, symbol_count: 2 } } }),
    ev({ event_type: 'target_computed', seq: 3, details: { decision: { curr_target: { rb2610: 0.9, ag2612: 0.9 } } } }),
    ev({
      event_type: 'order_terminal',
      seq: 4,
      status: 'SUCCESS',
      symbol: 'ag2612',
      ts_local_created: '2026-07-14T10:18:27',
      details: {
        order: { direction: 'OrderDirection.SELL', terminal_status: '已成交', filled_ratio: 1.0 },
        exchange: { client_order_id: 'lIww' },
      },
    }),
    ev({
      event_type: 'symbol_decision_made',
      seq: 6,
      status: 'SUCCESS',
      symbol: 'ag2612',
      details: { decision: { algorithm: 'SINGLE-MAKER', status: 'SUCCEEDED', target_volume: 2.599, orders_count: 1 } },
    }),
    ev({ event_type: 'execution_completed', seq: 8, status: 'SUCCESS', ts_local_created: '2026-07-14T10:18:50', details: { debug: { record_id: 9, success: true, execution_status: 'SUCCEEDED' } } }),
  ]
  const artifacts: ExecutionArtifact[] = [art('standard_input', {
      input: {
        curr_target: { ag2612: 0.9 },
        last_target: { ag2612: 1.5 },
        algorithm: { method: 'SINGLE-MAKER', params: { price_strategy: 'PASSIVE', chase_enabled: true, max_chase_count: 50, chase_interval: 5 } },
        forbidden_symbols: [],
        risk_symbols: [],
      },
    }),
    art('target_snapshot', { curr_target: { ag2612: 0.9 }, last_target: { ag2612: 1.5 } }),
    art('account_snapshot_before', {
      account_assets: { total_asset: 5147.62, market_value: 15620.7, positions: [{ symbol: 'ag2612', extra: { unrealized_pnl: 51.53 } }], source: 'real', update_time: '2026-07-14T10:18:08' },
      source: 'real',
    }),
    art('account_snapshot', {
      account_assets: { total_asset: 5176.1, market_value: 9265.26, positions: [{ symbol: 'ag2612', extra: { unrealized_pnl: 30.47 } }], source: 'real', update_time: '2026-07-14T10:18:49' },
    })]
  const summary: Record<string, unknown> = {
      status: 'SUCCEEDED',
      summary: { symbols_total: 1, symbols_succeeded: 1, symbols_failed: 0, symbols_noop: 0 },
      success: true,
      execution_time: 41.2,
      reconciliation: {
        account: { equity_before: 5147.62, equity_after: 5176.1, source_before: 'real', source_after: 'real' },
        symbols: [
          { symbol: 'ag2612', status: 'SUCCEEDED', target: 2.599, filled: -1.794, filled_value: -3198.0, avg_price: 1782.73, before: 4.393, after: 2.599, moved: -1.794, drift: 0, attained_ratio: 1.0, reached: true },
        ],
      },
    }
  return { events, artifacts, summary }
}

it('HKD 成交价与 CNY 审计金额保留各自币种', () => {
  const artifacts: ExecutionArtifact[] = []
  const summary: Record<string, unknown> = {
    reconciliation: {
      account: {},
      symbols: [{ symbol: '00700.HK', status: 'SUCCEEDED', filled: 3.9, filled_value: 5.915,
        value_currency: 'CNY', native_currency: 'HKD', avg_price: 1.5164 }],
    },
  }
  const model = buildExecutionDetail([], artifacts, null, summary)
  expect(model.symbols[0].filledValue).toBe(5.915)
  expect(model.symbols[0].valueCurrency).toBe('CNY')
  expect(model.symbols[0].avgPrice).toBe(1.5164)
  expect(model.symbols[0].priceCurrency).toBe('HKD')
})

describe('buildExecutionDetail · 头条', () => {
  function task(patch: Partial<ExecutionStatus>): ExecutionStatus {
    return {
      execution_id: 'e', account_id: 1, execution_kind: 'rebalance', status: 'RUNNING',
      created_at: '', started_at: null, finished_at: null, error: null,
      record_id: null, is_success: null, cancel_requested_at: null,
      cancel_reason: null, terminate_mode: null, ...patch,
    }
  }

  it.each([
    ['QUEUED', '等待执行'], ['RUNNING', '执行中'], ['TERMINATING', '正在终止'],
  ] as const)('%s 显示实时状态而非历史记录或提前完成', (status, text) => {
    for (const artifacts of [[], baseFixture().artifacts]) {
      const model = buildExecutionDetail([], artifacts, task({ status }))
      expect(executionHeadline(model)).toEqual({ level: 'neutral', text })
    }
  })

  it('摘要完成后发生错误，显示任务错误及其原因', () => {
    const model = buildExecutionDetail([], [], task({ status: 'FAILED', error: 'CTP 交易前置断线: 4097' }), {
      status: 'SUCCEEDED', error: '旧摘要原因', success: true,
    })
    expect(executionHeadline(model).text).toContain('执行失败')
    expect(model.failure?.raw).toContain('CTP 交易前置断线')
  })

  it('旧摘要缺少 status 时按字段回退到任务 output_status', () => {
    const model = buildExecutionDetail([], [], task({ status: 'SUCCEEDED', output_status: 'SUCCEEDED' }), { execution_time: 10, symbol_results: { A: { status: 'SUCCEEDED' } } })
    expect(model.conclusion.state).toBe('SUCCEEDED')
    expect(model.symbols.map(s => s.symbol)).toEqual(['A'])
    expect(executionHeadline(model).text).toBe('调仓完成')
  })

  it.each(['BLOCKED', 'PARTIAL'] as const)('FAILED 任务保留明确 %s 业务结果与品种证据', output_status => {
    const symbol_results = { A: { status: output_status, error: '业务原因', orders: [], trades: [] } }
    const model = buildExecutionDetail([], [], task({ status: 'FAILED', output_status, error: '业务原因' }), { status: output_status, error: '业务原因', symbol_results })
    expect(model.conclusion.state).toBe(output_status)
    expect(model.conclusion.symbols).toEqual(['A'])
    expect(model.symbols.map(s => s.symbol)).toEqual(['A'])
    expect(model.conclusion.reason).toBe('业务原因')
  })

  it('实际生命周期失败的 output_status 覆盖先前完成摘要，同时保留证据', () => {
    const model = buildExecutionDetail([], [], task({ status: 'FAILED', output_status: 'FAILED', error: '收尾失败' }), { status: 'SUCCEEDED', symbol_results: { A: { status: 'SUCCEEDED' } } })
    expect(model.conclusion.state).toBe('FAILED')
    expect(model.conclusion.reason).toBe('收尾失败')
    expect(model.symbols.map(s => s.symbol)).toEqual(['A'])
  })

  it('摘要完成后终止，显示任务终止结论', () => {
    const model = buildExecutionDetail([], baseFixture().artifacts, task({ status: 'TERMINATED' }), baseFixture().summary)
    expect(executionHeadline(model).text).toBe('执行已终止')
    expect(model.failure).toBeNull()
  })

  it('强制停止且挂单状态未确认时给出风险提示', () => {
    const events = [ev({
      event_type: 'execution_terminated',
      status: 'WARNING',
      details: { termination: { forced: true, cancel_unconfirmed: true } },
    })]

    expect(buildExecutionDetail(events, []).terminationWarning).toBe('worker 已强制停止，挂单状态未确认')
  })

  it('协作终止不显示强制停止风险提示', () => {
    const events = [ev({
      event_type: 'execution_terminated',
      status: 'WARNING',
      details: { termination: { forced: false, cancel_unconfirmed: false } },
    })]

    expect(buildExecutionDetail(events, []).terminationWarning).toBeNull()
  })

  it('status 失败在空事件和空附件时仍生成真实失败判词', () => {
    const task = {
      execution_id: 'e', account_id: 3, execution_kind: null, status: 'FAILED',
      created_at: '2026-08-25T11:32:05', started_at: null, finished_at: '2026-08-25T11:32:05',
      error: '调仓执行失败, 错误原因: CTP 交易前置断线: 4097', record_id: 36, is_success: 0,
      cancel_requested_at: null, cancel_reason: null, terminate_mode: null,
    } satisfies ExecutionStatus
    const model = buildExecutionDetail([], [], task)
    expect(model.failure?.raw).toContain('CTP 交易前置断线')
    expect(model.task?.started_at).toBeNull()
    expect(model.header.totalCount).toBe(0)
  })

  it('汇总触发/耗时/到位/权益/敞口/来源', () => {
    const { events, artifacts, summary } = baseFixture()
    const { header } = buildExecutionDetail(events, artifacts, null, summary)
    expect(header.kind).toBe('rebalance')
    expect(header.trigger).toBe('manual')
    expect(header.durationSec).toBe(41.2)
    expect(header.reachedCount).toBe(1)
    expect(header.totalCount).toBe(1)
    expect(header.equityBefore).toBe(5147.62)
    expect(header.equityAfter).toBe(5176.1)
    expect(Math.round(header.exposureBefore ?? 0)).toBe(303)
    expect(Math.round(header.exposureAfter ?? 0)).toBe(179)
    expect(header.sourcePosition).toBe('real')
    expect(header.sourceEquity).toBe('real')
    expect(header.success).toBe(true)
  })

  it('项失败按逐只成交结果计，不重复计入生命周期 ERROR 回声', () => {
    const { events, artifacts, summary } = baseFixture()
    // SR609 真失败（决策 ERROR、未到位/未知）
    events.push(
      ev({
        event_type: 'symbol_decision_made',
        seq: 7,
        status: 'ERROR',
        symbol: 'SR609',
        details: { decision: { algorithm: 'SINGLE-MAKER', status: 'FAILED', orders_count: 0 } },
      }),
    )
    summary.success = false
    ;(summary.reconciliation as { symbols: unknown[] }).symbols.push({
      symbol: 'SR609', status: 'FAILED', target: null, filled: 0, filled_value: 0, avg_price: null, before: 974.2, after: 974.2, moved: 0, drift: 0, attained_ratio: null, reached: null,
    })
    // 对账事件也标 ERROR —— 逐只失败的生命周期回声
    events.find((e) => e.event_type === 'execution_completed')!.status = 'ERROR'
    const header = buildExecutionDetail(events, artifacts, null, summary).header
    // 2 条 ERROR 事件（SR609 决策 + 对账回声），但只有 1 个品种真失败
    expect(events.filter((e) => e.status === 'ERROR')).toHaveLength(2)
    expect(header.failedCount).toBe(1)
    expect(header.success).toBe(false)
  })

  it('两只成交到位、两只不足一手时单独说明量化残差', () => {
    const { events, artifacts, summary } = baseFixture()
    for (const artifact of artifacts) artifact.execution_id = 'b780185776f64818bedff2e18b09abf7'
    const symbols = (summary.reconciliation as { symbols: Record<string, unknown>[] }).symbols
    symbols.splice(
      0,
      symbols.length,
      {
        symbol: 'TA701', status: 'SUCCEEDED', target: -1, filled: -1, filled_value: -27_810,
        avg_price: 5562, before: 0, after: -1, moved: -1, drift: 0, attained_ratio: 1, reached: true,
        sizing: {
          symbol: 'TA701', reason_code: 'COMMON.SIZING.QUANTIZED', strategy_weight: -0.0317,
          account_weight: -0.32, account_multiplier: 10.0946, equity: 99_973.5875,
          reference_price: 5564, unit_multiplier: 5, unit_notional: 27_820,
          target_notional: 31_991.548, raw_quantity: -1.14995, target_quantity: -1, quantity_step: 1,
        },
      },
      {
        symbol: 'rb2610', status: 'SUCCEEDED', target: 1, filled: 1, filled_value: 30_860,
        avg_price: 3086, before: 0, after: 1, moved: 1, drift: 0, attained_ratio: 1, reached: true,
        sizing: {
          symbol: 'rb2610', reason_code: 'COMMON.SIZING.QUANTIZED', strategy_weight: 0.0377,
          account_weight: 0.38, account_multiplier: 10.0796, equity: 99_973.5875,
          reference_price: 3086, unit_multiplier: 10, unit_notional: 30_860,
          target_notional: 37_989.963, raw_quantity: 1.23104, target_quantity: 1, quantity_step: 1,
        },
      },
      {
        symbol: 'm2701', status: 'NOOP', target: 0, filled: 0, filled_value: 0,
        avg_price: null, before: 0, after: 0, moved: 0, drift: 0, attained_ratio: 1, reached: true,
        sizing: {
          symbol: 'm2701', reason_code: 'COMMON.SIZING.BELOW_MIN_QUANTITY', strategy_weight: -0.0175,
          account_weight: -0.18, account_multiplier: 10.2857, equity: 99_973.5875,
          reference_price: 3336, unit_multiplier: 10, unit_notional: 33_360,
          target_notional: 17_995.246, raw_quantity: -0.53943, target_quantity: 0, quantity_step: 1,
        },
      },
      {
        symbol: 'c2611', status: 'NOOP', target: 0, filled: 0, filled_value: 0,
        avg_price: null, before: 0, after: 0, moved: 0, drift: 0, attained_ratio: 1, reached: true,
        sizing: {
          symbol: 'c2611', reason_code: 'COMMON.SIZING.BELOW_MIN_QUANTITY', strategy_weight: 0.01,
          account_weight: 0.1, account_multiplier: 10, equity: 99_973.5875,
          reference_price: 2286, unit_multiplier: 10, unit_notional: 22_860,
          target_notional: 9_997.359, raw_quantity: 0.43733, target_quantity: 0, quantity_step: 1,
        },
      },
    )
    events.push(
      ev({ event_type: 'symbol_skipped', symbol: 'm2701', reason_code: 'COMMON.SIZING.BELOW_MIN_QUANTITY' }),
      ev({ event_type: 'symbol_skipped', symbol: 'c2611', reason_code: 'COMMON.SIZING.BELOW_MIN_QUANTITY' }),
    )

    const model = buildExecutionDetail(events, artifacts, null, summary)

    expect(model.header.tradedReachedCount).toBe(2)
    expect(model.header.quantizedZeroCount).toBe(2)
    expect(model.symbols.find((symbol) => symbol.symbol === 'm2701')?.action).toBe('aligned')
    expect(model.symbols.find((symbol) => symbol.symbol === 'm2701')?.broken).toBe(false)
    expect(executionHeadline(model).text).toBe('调仓完成')
  })
})

describe('formatOrderTradeCounts', () => {
  it('有成交明细时数笔', () => {
    expect(formatOrderTradeCounts(1, 1, true)).toBe('1 单 1 成交')
    expect(formatOrderTradeCounts(1, 1, true, ' · ')).toBe('1 单 · 1 成交')
  })

  it('订单已成但明细缺失时不喊 0 成交', () => {
    expect(formatOrderTradeCounts(1, 0, true)).toBe('1 单')
    expect(formatOrderTradeCounts(1, 0, true, ' · ')).toBe('1 单')
  })

  it('确实没成交时才写 0 成交', () => {
    expect(formatOrderTradeCounts(1, 0, false)).toBe('1 单 0 成交')
    expect(formatOrderTradeCounts(1, 0, false, ' · ')).toBe('1 单 · 0 成交')
  })
})

describe('buildExecutionDetail · 目标变化', () => {
  it('展示目标持仓算法的开平仓订单类型', () => {
    const { events, artifacts, summary } = baseFixture()
    const standardInput = artifacts.find(artifact => artifact.artifact_type === 'standard_input')!
    const input = standardInput.content.input as Record<string, unknown>
    input.algorithm = { method: 'TARGET-POS-TASK', params: { close_order_type: 'MARKET', open_order_type: 'LIMIT', price_strategy: 'PASSIVE' } }
    const tc = buildExecutionDetail(events, artifacts, null, summary).targetChange
    expect(tc?.algoParams).toContain('平仓市价')
    expect(tc?.algoParams).toContain('开仓限价')
  })
  it('给出 last→curr 与算法参数摘要', () => {
    const { events, artifacts, summary } = baseFixture()
    const tc = buildExecutionDetail(events, artifacts, null, summary).targetChange
    expect(tc?.rows).toEqual([{ symbol: 'ag2612', curr: 0.9, last: 1.5, strategy: null }])
    expect(tc?.algorithm).toBe('SINGLE-MAKER')
    expect(tc?.algoParams).toContain('PASSIVE')
    expect(tc?.algoParams).toContain('追价')
  })
})

describe('buildExecutionDetail · 逐只子链', () => {
  it('TCA 与订单树透传到 SymbolChain', () => {
    const { events, artifacts, summary } = baseFixture()
    const ag0 = (summary.reconciliation as { symbols: Record<string, unknown>[] }).symbols[0]!
    ag0.tca = { n_orders: 1, n_trades: 1, arrival_mid: 1782.66, slippage_bps: 0.36, liquidity: 'passive', fill_ratio: 1, fee: 0.64, fee_asset: 'CNY' }
    ag0.orders = [{ order_id: 'o1', side: 'sell', order_type: 'LIMIT', price: 1782.73, avg_price: 1782.73, volume: 1.794, filled_volume: 1.794, status: '已成交', client_order_id: 'lIww', trades: [{ price: 1782.73, volume: 1.794, value: 3198, time: '2026-07-14T10:18:26', fee: 0.64, fee_asset: 'CNY' }] }]
    const ag = buildExecutionDetail(events, artifacts, null, summary).symbols[0]!
    expect(ag.tca?.liquidity).toBe('passive')
    expect(ag.tca?.slippage_bps).toBe(0.36)
    expect(ag.tca?.fee).toBe(0.64)
    expect(ag.orders).toHaveLength(1)
    expect(ag.orders[0]?.trades[0]?.fee).toBe(0.64)
  })

  it('减仓到位：方向/成交/均价/浮盈/耗时/无断点', () => {
    const { events, artifacts, summary } = baseFixture()
    const ag = buildExecutionDetail(events, artifacts, null, summary).symbols[0]!
    expect(ag.action).toBe('reduce')
    expect(ag.side).toBe('sell')
    expect(ag.filled).toBe(-1.794)
    expect(ag.filledValue).toBe(-3198.0)
    expect(ag.avgPrice).toBe(1782.73)
    expect(ag.reached).toBe(true)
    expect(ag.legSeconds).toBe(19)
    expect(ag.clientOrderId).toBe('lIww')
    expect(ag.pnlBefore).toBe(51.53)
    expect(ag.pnlAfter).toBe(30.47)
    expect(ag.broken).toBe(false)
  })

  it('撤销欠量：终态撤销 + reached=false → 断点变红并给原因', () => {
    const { events, artifacts, summary } = baseFixture()
    // 改成 rb2610 撤销、欠量
    events.push(
      ev({
        event_type: 'order_terminal',
        seq: 5,
        status: 'WARNING',
        symbol: 'rb2610',
        details: { order: { direction: 'OrderDirection.BUY', terminal_status: '已撤销', filled_ratio: 0.74 }, exchange: { client_order_id: 'C4' } },
      }),
    )
    ;(summary.reconciliation as { symbols: unknown[] }).symbols.push({
      symbol: 'rb2610', status: 'PARTIAL', target: 0.1243, filled: 0.0675, filled_value: 4197, avg_price: 62174, before: 0.0336, after: 0.1011, moved: 0.0675, drift: 0, attained_ratio: 0.81, reached: false,
    })
    const rb = buildExecutionDetail(events, artifacts, null, summary).symbols.find((s) => s.symbol === 'rb2610')!
    expect(rb.terminalStatus).toBe('已撤销')
    expect(rb.reached).toBe(false)
    expect(rb.broken).toBe(true)
    expect(rb.reason).toContain('撤')
  })

  it('保留历史到位证据，但不让它覆盖明确 BLOCKED 状态', () => {
    const { events, artifacts, summary } = baseFixture()
    // 该动的量小到低于最小可交易粒度 → 决策 BLOCKED、未成交，但仓位天然在目标上。
    events.push(
      ev({
        event_type: 'symbol_decision_made',
        seq: 7,
        status: 'WARNING',
        symbol: 'm2609',
        details: { decision: { algorithm: 'SINGLE-MAKER', status: 'BLOCKED', orders_count: 0 } },
      }),
    )
    ;(summary.reconciliation as { symbols: unknown[] }).symbols.push({
      symbol: 'm2609', status: 'BLOCKED', error: '受阻', target: 2.372859, filled: 0, filled_value: 0, avg_price: null, before: 2.37, after: 2.37, moved: 0, drift: 0, attained_ratio: 0.9988, reached: true,
    })
    const m = buildExecutionDetail(events, artifacts, null, summary).symbols.find((s) => s.symbol === 'm2609')!
    expect(m.reached).toBe(true)
    expect(m.action).not.toBe('failed')
    expect(m.broken).toBe(true)
    expect(m.status).toBe('BLOCKED')
    expect(m.reason).toBe('受阻')
  })

  it('受阻且未到位：显示断点但不判过程失败，原因给「受阻」', () => {
    const { events, artifacts, summary } = baseFixture()
    events.push(
      ev({
        event_type: 'symbol_decision_made',
        seq: 7,
        status: 'WARNING',
        symbol: 'm2609',
        details: { decision: { algorithm: 'SINGLE-MAKER', status: 'BLOCKED', orders_count: 0 } },
      }),
    )
    ;(summary.reconciliation as { symbols: unknown[] }).symbols.push({
      symbol: 'm2609', status: 'BLOCKED', error: '受阻', target: 3, filled: 0, filled_value: 0, avg_price: null, before: 1, after: 1, moved: 0, drift: 0, attained_ratio: 0.33, reached: false,
    })
    const m = buildExecutionDetail(events, artifacts, null, summary).symbols.find((s) => s.symbol === 'm2609')!
    expect(m.reached).toBe(false)
    expect(m.action).not.toBe('failed')
    expect(m.broken).toBe(true)
    expect(m.reason).toBe('受阻')
  })

  it('真失败且结果未知：决策 FAILED、reached=null → 判失败并断点，原因行不回声英文/「失败」', () => {
    const { events, artifacts, summary } = baseFixture()
    events.push(
      ev({
        event_type: 'symbol_decision_made',
        seq: 7,
        status: 'ERROR',
        symbol: 'SR609',
        details: { decision: { algorithm: 'SINGLE-MAKER', status: 'FAILED', orders_count: 0 } },
      }),
    )
    ;(summary.reconciliation as { symbols: unknown[] }).symbols.push({
      symbol: 'SR609', status: 'FAILED', target: null, filled: 0, filled_value: 0, avg_price: null, before: 974.2, after: 974.2, moved: 0, drift: 0, attained_ratio: null, reached: null,
    })
    const sr = buildExecutionDetail(events, artifacts, null, summary).symbols.find((s) => s.symbol === 'SR609')!
    expect(sr.reached).toBeNull()
    expect(sr.action).toBe('failed')
    expect(sr.broken).toBe(true)
    // FAILED 与头部「失败」同义反复 → 原因行留空，不吐 FAILED/失败
    expect(sr.reason).toBe('')
  })

  it('跳过：symbol_skipped → action=skipped + 原因 + 断点', () => {
    const { events, artifacts, summary } = baseFixture()
    events.push(ev({ event_type: 'symbol_skipped', symbol: 'au2612', status: 'WARNING', reason_code: 'RISK.FORBIDDEN' }))
    ;(summary.reconciliation as { symbols: unknown[] }).symbols.push({
      symbol: 'au2612', status: 'SKIPPED', target: 1, filled: 0, filled_value: 0, avg_price: null, before: 0, after: 0, moved: 0, drift: 0, attained_ratio: 0, reached: false,
    })
    const au = buildExecutionDetail(events, artifacts, null, summary).symbols.find((s) => s.symbol === 'au2612')!
    expect(au.action).toBe('skipped')
    expect(au.broken).toBe(true)
    expect(au.reason).toBe('受账户交易限制')
  })

  it('已到位的 symbol_skipped 仅保留原因，不标成断点', () => {
    const { events, artifacts, summary } = baseFixture()
    events.push(ev({ event_type: 'symbol_skipped', symbol: 'rb2610', reason_code: 'RISK.FORBIDDEN' }))
    ;(summary.reconciliation as { symbols: unknown[] }).symbols.push({
      symbol: 'rb2610', status: 'NOOP', target: 1, filled: 0, filled_value: 0, avg_price: null,
      before: 1, after: 1, moved: 0, drift: 0, attained_ratio: 1, reached: true,
    })

    const rb = buildExecutionDetail(events, artifacts, null, summary).symbols.find((s) => s.symbol === 'rb2610')!
    expect(rb.action).toBe('aligned')
    expect(rb.broken).toBe(false)
    expect(rb.reason).toBe('受账户交易限制')
  })
})

describe('buildExecutionDetail · 账户书签（证据）', () => {
  it('从前后快照与输入取现金/权益/市值/超时', () => {
    const { events, artifacts, summary } = baseFixture()
    const before = artifacts.find((a) => a.artifact_type === 'account_snapshot_before')!
    ;(before.content.account_assets as Record<string, unknown>).available_cash = 4441.17
    const after = artifacts.find((a) => a.artifact_type === 'account_snapshot')!
    ;(after.content.account_assets as Record<string, unknown>).available_cash = 4755.75
    const std = artifacts.find((a) => a.artifact_type === 'standard_input')!
    ;(std.content.input as Record<string, unknown>).execution_timeout = 60
    const b = buildExecutionDetail(events, artifacts, null, summary).bookends
    expect(b.cashBefore).toBe(4441.17)
    expect(b.cashAfter).toBe(4755.75)
    expect(b.equityBefore).toBe(5147.62)
    expect(b.mvAfter).toBe(9265.26)
    expect(b.mvAfterReference).toBe(false)
    const afterAssets = after.content.account_assets as { positions: { extra: Record<string, unknown> }[] }
    afterAssets.positions[0].extra.market_value_provenance = {
      source: 'ctp_first_tick_last_price_0014', quote_timestamp: 1780000000000,
    }
    expect(buildExecutionDetail(events, artifacts, null, summary).bookends.mvAfterReference).toBe(true)
    expect(b.timeoutSec).toBe(60)
  })
})

describe('buildExecutionDetail · 脊柱与降级', () => {
  it('脊柱含各节点、全 real 时不断且不吐裸枚举', () => {
    const { events, artifacts, summary } = baseFixture()
    const spine = buildExecutionDetail(events, artifacts, null, summary).spine
    const byKey = Object.fromEntries(spine.map((n) => [n.key, n]))
    // 逐只执行拆成阶段（这次两只都减仓 → 只有阶段1）
    expect(spine.map((n) => n.key)).toEqual(['started', 'input', 'before', 'target', 'phase1', 'after', 'completed'])
    expect(spine.every((n) => !n.broken)).toBe(true)
    // real 时账户节点保持安静（不显示 real）
    expect(byKey.before?.detail).toBe('')
    expect(byKey.after?.detail).toBe('')
    // 算目标不塌成单一值，只报变更方向
    expect(byKey.target?.detail).toBe('目标下调')
    // 阶段节点标并行、带到位
    expect(byKey.phase1?.label).toBe('阶段1 减仓')
    expect(byKey.phase1?.detail).toContain('并行 1 只')
    expect(byKey.phase1?.detail).toContain('1/1 到位')
    // 对账用人话、不吐 SUCCEEDED / #record_id
    expect(byKey.completed?.label).toBe('调仓完成')
  })

  it('有减有增 → 拆成 阶段1 减仓 + 阶段2 开仓（阶段1 全成才跑）', () => {
    const { events, artifacts, summary } = baseFixture()
    ;(summary.reconciliation as { symbols: unknown[] }).symbols.push({
      symbol: 'au2612', status: 'SUCCEEDED', target: 10, filled: 10, filled_value: 1000, avg_price: 100, before: 0, after: 10, moved: 10, drift: 0, attained_ratio: 1, reached: true,
    })
    const spine = buildExecutionDetail(events, artifacts, null, summary).spine
    const byKey = Object.fromEntries(spine.map((n) => [n.key, n]))
    expect(byKey.phase1?.label).toBe('阶段1 减仓') // ag2612 减仓
    expect(byKey.phase2?.label).toBe('阶段2 开仓') // au2612 建仓
    expect(byKey.phase2?.detail).toContain('阶段1 全成才跑')
    expect(byKey.target?.detail).toBe('目标下调') // 目标快照里只有 ag2612（下调）
  })

  it('执行前读账户降级：before 非 real → 只污染权益族，持仓族(到位度)仍为 real', () => {
    const { events, artifacts, summary } = baseFixture()
    const before = artifacts.find((a) => a.artifact_type === 'account_snapshot_before')!
    before.content.source = 'assumed'
    ;(before.content.account_assets as { source: string }).source = 'assumed'
    ;(summary.reconciliation as { account: { source_before: string } }).account.source_before = 'assumed'
    const model = buildExecutionDetail(events, artifacts, null, summary)
    // 权益族取两端较差 → assumed；到位度只依赖执行后快照 → 仍 real，不被 before 缺失污染。
    expect(model.header.sourceEquity).toBe('assumed')
    expect(model.header.sourcePosition).toBe('real')
    const beforeNode = model.spine.find((n) => n.key === 'before')
    expect(beforeNode?.broken).toBe(true)
    expect(beforeNode?.detail).toBe('假设值')
  })

  it('执行后快照降级：after 非 real → 持仓族与权益族均退化', () => {
    const { events, artifacts, summary } = baseFixture()
    ;(summary.reconciliation as { account: { source_after: string } }).account.source_after = 'error'
    const model = buildExecutionDetail(events, artifacts, null, summary)
    expect(model.header.sourcePosition).toBe('error')
    expect(model.header.sourceEquity).toBe('error')
  })
})

it('未到位详情不受旧任务 FAILED 与汇总错误回声影响', () => {
  const { events, artifacts, summary } = baseFixture()
  summary.status = 'PARTIAL'
  summary.symbol_results = { ag2612: { status: 'PARTIAL' } }
  const row = (summary.reconciliation as { symbols: Record<string, unknown>[] }).symbols[0]
  Object.assign(row, { status: 'PARTIAL', target: 3, after: 2, final_volume: 2, error: '执行时限结束', reached: false })
  const model = buildExecutionDetail([...events, ev({ event_type: 'execution_failed', status: 'ERROR' })], artifacts, null, summary)
  expect(executionHeadline(model).text).toBe('执行未全部完成')
  expect(model.failure).toBeNull()
  expect(model.symbols[0]).toMatchObject({ target: 3, after: 2, reached: false, reason: '执行时限结束' })
  expect(model.symbols[0].action).not.toBe('failed')
  expect(model.spine.find(n => n.key === 'failed')?.label).toBe('执行未全部完成')
})

it('旧记录保留原始附件和事件，不推断失败标题', () => {
  const { events, artifacts, summary } = baseFixture()
  delete summary.status
  const model = buildExecutionDetail([...events, ev({ event_type: 'execution_failed', status: 'ERROR' })], artifacts, null, summary)
  expect(executionHeadline(model).text).toBe('执行状态未知')
  expect(model.failure).toBeNull()
  expect(model.artifacts).toBe(artifacts)
})

it.each([true, false, null])('PARTIAL 保留持仓到位证据 %s，执行错误单独展示', (reached) => {
  const model = buildExecutionDetail([], [], null, {
    status: 'PARTIAL', error: '撤单失败，订单终态尚未确认',
    symbol_results: { A: { status: 'PARTIAL', error: '撤单失败，订单终态尚未确认', target_volume: 2 } },
    reconciliation: { account: { source_before: 'real', source_after: 'real' }, symbols: [
      { symbol: 'A', before: 0, after: reached === true ? 2 : 1, target: 2, reached, attained_ratio: reached === true ? 1 : 0.5 },
    ] },
  })
  expect(model.conclusion.state).toBe('PARTIAL')
  expect(model.failure?.human).toBe('撤单失败，订单终态尚未确认')
  expect(model.symbols[0].reached).toBe(reached)
  expect(model.symbols[0].reason).toBe('撤单失败，订单终态尚未确认')
})

it('最终账户查询失败不把旧摘要占位零持仓当成清仓完成', () => {
  const model = buildExecutionDetail([], [], null, {
    status: 'PARTIAL', symbol_results: { A: { status: 'PARTIAL', target_volume: 0, final_volume: null } },
    reconciliation: { account: { source_before: 'real', source_after: 'unavailable' }, symbols: [
      { symbol: 'A', before: 2, after: 0, target: 0, reached: true, attained_ratio: 1 },
    ] },
  })
  expect(model.symbols[0]).toMatchObject({ observedAfter: null, reached: null, attainedRatio: null })
})

it('自定义仿真快照保留持仓和到位证据', () => {
  const model = buildExecutionDetail([], [], null, {
    status: 'SUCCEEDED', symbol_results: { A: { status: 'SUCCEEDED', target_volume: 2 } },
    reconciliation: { account: { source_before: 'simulation', source_after: 'simulation' }, symbols: [
      { symbol: 'A', before: 0, after: 2, target: 2, reached: true, attained_ratio: 1 },
    ] },
  })
  expect(model.symbols[0]).toMatchObject({ observedBefore: 0, observedAfter: 2, reached: true, attainedRatio: 1 })
  expect(model.header).toMatchObject({ sourcePosition: 'simulation', sourceEquity: 'simulation' })
})

it('最新真实快照与算法先前持仓不同时，数量和到位度使用同一次对账', () => {
  const model = buildExecutionDetail([], [], null, {
    status: 'SUCCEEDED', symbol_results: { A: { status: 'SUCCEEDED', target_volume: 2, final_volume: 2 } },
    reconciliation: { account: { source_before: 'real', source_after: 'real' }, symbols: [
      { symbol: 'A', before: 0, after: 3, target: 2, reached: false, attained_ratio: 1.5 },
    ] },
  })
  expect(model.symbols[0]).toMatchObject({ observedAfter: 3, after: 3, reached: false, attainedRatio: 1.5 })
})

it('详情头条与品种原因展示后端说明，原始附件保留', () => {
  const summary = {
    status: 'BLOCKED', error: '非交易时段', reason_code: 'COMMON.SESSION.CLOSED',
    reconciliation: { account: { source_before: 'real', source_after: 'real' }, symbols: [{ symbol: 'm2609', status: 'BLOCKED', error: '非交易时段', before: 0, after: 0, target: 1 }] },
  }
  const model = buildExecutionDetail([], [], null, summary)
  expect(executionHeadline(model).text).toBe('休市跳过')
  expect(model.symbols[0].reason).toBe('非交易时段')
  expect(summary.error).toBe('非交易时段')
})

it('详情保留未知原因，不猜测或隐藏诊断信息', () => {
  const model = buildExecutionDetail([], [], null, { status: 'FAILED', error: 'UNKNOWN_CODE' })
  expect(model.conclusion.reason).toBe('UNKNOWN_CODE')
  expect(JSON.stringify(model.failure)).toContain('UNKNOWN_CODE')
})
