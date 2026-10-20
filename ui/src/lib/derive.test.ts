import { expect, test } from 'bun:test'
import { integrityOf, needsAdjustmentAttention, gateOf, observedTotalAsset, positionsOf, rebalancePlan, stateVerdict } from './derive'
import type { AccountDashboardItem, ExecuteRecord, Position } from '../types/api'

/** 造一个仪表盘项；只填两轴关心的字段，其余给中性占位。 */
function item(over: Partial<AccountDashboardItem>): AccountDashboardItem {
  return {
    account_id: 1,
    name: 'testnet',
    remark: null,
    market: 'demo-market',
    trade_channel: 'paper',
    is_started: true,
    portfolio_id: null,
    is_scheduled: false,
    next_run_time: null,
    total_asset: 0,
    currency: 'CNY',
    holdings_count: 0,
    position_weights: [],
    last_is_success: null,
    last_exec_at: null,
    last_output_status: null,
    off_symbol_count: null,
    ...over,
  }
}

// ── 在位性（风险轴）：只由持仓 vs 目标派生 ──

test('off_symbol_count=0 → 在位（不看 last_is_success）', () => {
  expect(integrityOf(item({ off_symbol_count: 0, last_is_success: 0, last_exec_at: '2026-07-21T12:00:00' })).integrity).toBe('aligned')
})

test('off_symbol_count>0 → 偏离（不看 last_is_success）', () => {
  const status = integrityOf(item({ off_symbol_count: 5, last_is_success: 1, last_exec_at: '2026-07-21T12:53:25' }))
  expect(status.integrity).toBe('off')
  expect(status.text).toBe('5 只待调整')
})

test('无记录且无数 → 未知（不无证推定良好）', () => {
  expect(integrityOf(item({ last_is_success: null, last_exec_at: null, off_symbol_count: null })).integrity).toBe('unknown')
})

test('有执行但无数 → 未知', () => {
  expect(integrityOf(item({ last_is_success: 0, last_exec_at: '2026-07-21T12:00:00', off_symbol_count: null })).integrity).toBe('unknown')
})

// ── 档位（模式轴）：只由 is_started 派生 ──

test('is_started=true → 自动', () => {
  expect(gateOf(item({ is_started: true })).gate).toBe('auto')
})

test('is_started=false → 暂停', () => {
  expect(gateOf(item({ is_started: false })).gate).toBe('paused')
})

// ── 正交性：暂停保留偏离事实，但不要求立即调仓 ──

test('暂停 + 偏离：档位=暂停 且 在位性=偏离（两轴各说各的）', () => {
  const it = item({ is_started: false, off_symbol_count: 5, last_exec_at: '2026-07-21T12:53:25' })
  expect(gateOf(it).gate).toBe('paused')
  expect(integrityOf(it).integrity).toBe('off')
})

// ── 四象限判词：模式给风险改台词 ──

test('自动 + 偏离 + 盘中失败 + 低敞口 → 会自动重试（不加重）', () => {
  const v = stateVerdict(item({
    is_started: true,
    off_symbol_count: 2,
    last_output_status: 'FAILED',
    last_is_success: 0,
    last_exec_at: '2026-07-21T12:53:25',
    total_asset: 1000,
    position_weights: [200],
  }))
  expect(v.text).toBe('上次未到位 · 将自动重试')
  expect(v.loud).toBe(false)
})

test('自动 + 偏离 + 盘中失败 + 高杠杆 → 敞口偏高（加重）', () => {
  const v = stateVerdict(item({
    is_started: true,
    off_symbol_count: 2,
    last_output_status: 'FAILED',
    last_is_success: 0,
    last_exec_at: 't',
    total_asset: 1000,
    position_weights: [900, 700],
  }))
  expect(v.text).toContain('敞口偏高')
  expect(v.loud).toBe(true)
})

test('暂停 + 偏离 → 中性说明，不要求手动调仓', () => {
  const v = stateVerdict(item({ is_started: false, off_symbol_count: 5, last_exec_at: '2026-07-21T12:53:25' }))
  expect(v.text).toBe('5 只偏离目标 · 账户已暂停')
  expect(v.loud).toBe(false)
  expect(stateVerdict(item({ is_started: false, off_symbol_count: 5, last_exec_at: '2026-07-21T12:53:25' }), false).text)
    .toBe('偏离目标 · 账户已暂停')
})

test('自动 + 偏离 + BLOCKED → 下次排程，不说失败、不加重', () => {
  const v = stateVerdict(item({
    is_started: true,
    off_symbol_count: 5,
    last_output_status: 'BLOCKED',
    last_is_success: 0,
    next_run_time: '2026-08-26T21:15:00+08:00',
    total_asset: 1000,
    position_weights: [200],
  }))
  expect(v.text).toContain('5 只待调整')
  expect(v.text).toContain('等待下次')
  expect(v.text).toContain('21:15')
  expect(v.loud).toBe(false)
})

test('在位 / 未知 与档位无关、皆不加重', () => {
  expect(stateVerdict(item({ is_started: false, off_symbol_count: 0, last_exec_at: 't' })).loud).toBe(false)
  expect(stateVerdict(item({ is_started: false, off_symbol_count: null })).loud).toBe(false)
})

const TONIGHT_EQUITY = 992_670.6124999999
const TONIGHT_POSITIONS: Position[] = [
  { symbol: 'c2611', volume: 1, market_value: 22800, direction: '多头', extra: { net_position: 1 } },
  { symbol: 'rb2610', volume: 4, market_value: 123100, direction: '多头', extra: { net_position: 4 } },
  { symbol: 'TA701', volume: 2, market_value: 55000, direction: '空头', extra: { net_position: -2 } },
  { symbol: 'm2701', volume: 1, market_value: 32960, direction: '空头', extra: { net_position: -1 } },
]
const TONIGHT_WEIGHTS = { TA701: -0.08, c2611: 0.03, m2701: -0.06, rb2610: 0.13 }
const TONIGHT_QTY = { TA701: -2, c2611: 1, m2701: -1, rb2610: 4 }

test('有 quantities 时手数到位不算待调整（权重余数忽略）', () => {
  const plan = rebalancePlan(TONIGHT_POSITIONS, TONIGHT_WEIGHTS, TONIGHT_EQUITY, TONIGHT_QTY)
  expect(plan.off).toBe(0)
  expect(plan.buys).toBe(0)
  expect(plan.sells).toBe(0)
})

test('无 quantities 时仍用权重阈值（不写渠道公式）', () => {
  const plan = rebalancePlan(TONIGHT_POSITIONS, TONIGHT_WEIGHTS, TONIGHT_EQUITY)
  expect(plan.off).toBe(4)
})

test('有 quantities 时 1 手可交易差额计待调整', () => {
  const positions: Position[] = [
    { symbol: 'rb2610', volume: 3, market_value: 92310, direction: '多头', extra: { net_position: 3 } },
  ]
  const plan = rebalancePlan(positions, { rb2610: 0.13 }, TONIGHT_EQUITY, { rb2610: 4 })
  expect(plan.off).toBe(1)
  expect(plan.buys).toBe(1)
})

test('观测权益优先快照，0 也是有效值', () => {
  expect(observedTotalAsset({ total_asset: 880_000 }, 995_215.24)).toBe(880_000)
  expect(observedTotalAsset({ total_asset: 0 }, 995_215.24)).toBe(0)
  expect(observedTotalAsset({ positions: [] }, 995_215.24)).toBe(995_215.24)
  expect(observedTotalAsset(undefined, 995_215.24)).toBe(995_215.24)
  expect(observedTotalAsset(undefined, undefined)).toBe(0)
})

test('类型化执行结果跳过缺失持仓，明确空数组仍代表观测到的空仓', () => {
  const record = (id: number, result: ExecuteRecord['result']): ExecuteRecord => ({
    id, account_id: 1, execution_id: `execution-${id}`, created_at: '2026-10-20T09:00:00', is_success: 1, input: null, result,
  })
  const positions: Position[] = [{ symbol: 'rb2610', volume: 1, market_value: null }]
  const older = record(1, { account_assets: { positions } })
  expect(positionsOf([record(2, {}), older])).toEqual(positions)
  for (const result of [null, [], '历史结果', 0, false]) expect(positionsOf([record(2, result), older])).toEqual(positions)
  expect(positionsOf([record(2, { account_assets: { positions: [] } }), older])).toEqual([])
  expect(positionsOf([record(2, {})])).toEqual([])
  expect(positions[0].market_value).toBeNull()
})

test('调仓提醒随暂停和恢复切换，保留实际偏离事实', () => {
  const paused = item({ is_started: false, off_symbol_count: 5 })
  expect(integrityOf(paused).integrity).toBe('off')
  expect(needsAdjustmentAttention(paused)).toBe(false)
  expect(needsAdjustmentAttention({ ...paused, is_started: true })).toBe(true)
  expect(needsAdjustmentAttention(item({ off_symbol_count: 0 }))).toBe(false)
  expect(needsAdjustmentAttention(item({ off_symbol_count: null }))).toBe(false)
})

test('暂停账户数据不足只说明已暂停，不催促查询或执行', () => {
  for (const last_exec_at of [null, '2026-07-21T12:00:00']) {
    const v = stateVerdict(item({ is_started: false, off_symbol_count: null, last_exec_at }))
    expect(v).toEqual({ integrity: 'unknown', text: '已暂停', loud: false })
  }
})

test('运行账户数据不足时说明等待阶段，不暗示异常', () => {
  expect(stateVerdict(item({ off_symbol_count: null })).text).toBe('等待首次对账')
  expect(stateVerdict(item({ off_symbol_count: null, last_exec_at: 't' })).text).toBe('持仓待查询')
  expect(stateVerdict(item({ off_symbol_count: null, last_exec_at: 't', asset_observed_at: 't' })).text)
    .toBe('等待目标与持仓对账')
})
