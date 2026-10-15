import { describe, expect, it } from 'bun:test'
import { holdingActions, holdingBook, holdingPreviewRows, holdingTarget, projectHolding, sameHoldingSymbol } from '@/features/account/holdingDetailModel'
import type { CancelReceipt, CurrentOrder, CurrentOrdersSnapshot } from '@/lib/api/orders'
import type { Position, TargetSizingRow, TargetWeightSnapshot } from '@/types/api'

const at = '2026-10-15T14:48:00'
const positions: Position[] = [{ symbol: 'c2611', direction: '空头', volume: 18, available_volume: 17,
  market_value: 540_000, extra: { short_td: 3, short_yd: 15, long_td: 0, long_yd: 0, net_position: -18 } }]
const evidence: TargetSizingRow = { symbol: 'c2611', sizing_mode: 'weight', status: 'SIZED', reason_code: 'COMMON.SIZING.EXACT',
  strategy_weight: -0.3, account_weight: -0.6, account_multiplier: 2, weight_precision: 0.01,
  equity: 1_000_000, reference_price: 3000, unit_multiplier: 10, unit_notional: 30_000,
  target_notional: 600_000, raw_quantity: -20, target_quantity: -20, current_quantity: -18,
  quantity_step: 1, min_quantity: 1, min_notional: null }
const target: TargetWeightSnapshot = { weights: { c2611: -0.6, m2701: 0.2 }, calculated_at: at, source: 'execution',
  execution_id: 'exec-1', context_account_id: 1,
  sizing: { status: 'available', execution_id: 'exec-1', calculated_at: at, rows: { c2611: evidence } } }
const order: CurrentOrder = { order_id: 'c-1', symbol: 'c2611', direction: 'SELL', offset: 'OPEN', order_type: 'LIMIT',
  price: 3001, volume: 2, filled_volume: 1, remaining_volume: 1, status: '部分成交', create_time: at,
  execution_id: 'exec-1', native_identity: {}, uncertain: false }
const snapshot: CurrentOrdersSnapshot = { data: [order], count: 1, complete: true, sampled_at: at, cancellations: [] }
const receipt: CancelReceipt = { request_id: 'request-1', order_id: order.order_id, symbol: order.symbol,
  state: 'submitted', message: '已提交', requested_at: at, order }

function project(patch: Partial<Parameters<typeof projectHolding>[0]> = {}) {
  return projectHolding({ book: holdingBook(positions, 'c2611', true), target: -20, orders: snapshot,
    receipts: [], observedAt: at, stale: false, ...patch })
}

describe('品种身份与持仓入口', () => {
  it('追加目标新开仓品种，四列仍显示当前持仓而非目标数量', () => {
    const rows = holdingPreviewRows(positions, 1_000_000, target, true)
    expect(rows.map(row => row.symbol)).toEqual(['c2611', 'm2701'])
    expect(rows[1]).toMatchObject({ targetOnly: true, weight: 0, volume: 0, availableVolume: 0, value: null })
    expect(holdingPreviewRows([], 0, target, false)[0].volume).toBeNull()
  })
  it('同时保留多空行和清仓品种，不重复追加同品种目标', () => {
    const rows = holdingPreviewRows([...positions, { symbol: 'c2611', direction: '多头', volume: 3, market_value: 90_000 },
      { symbol: 'ag2612', direction: '多头', volume: 2, market_value: 100_000 }], 1_000_000, target, true)
    expect(rows.filter(row => row.symbol === 'c2611')).toHaveLength(2)
    expect(rows.some(row => row.symbol === 'ag2612')).toBe(true)
    expect(rows.filter(row => row.targetOnly)).toHaveLength(1)
  })
  it('限定代码可匹配本地合约，不跨交易所或股票交易所混合', () => {
    expect(sameHoldingSymbol('SHFE.rb2610', 'rb2610')).toBe(true)
    expect(sameHoldingSymbol('SHFE.rb2610', 'DCE.rb2610')).toBe(false)
    expect(sameHoldingSymbol('SSE.000001', 'SZSE.000001')).toBe(false)
    expect(sameHoldingSymbol('SSE.000001', '000001')).toBe(false)
    expect(sameHoldingSymbol('rb2610', 'ag2612')).toBe(false)
  })
})

describe('目标依据', () => {
  it('优先冻结的目标数量，不使用最新市值重新计算', () => {
    expect(holdingTarget(target, 'DCE.c2611').quantity).toBe(-20)
    expect(holdingTarget({ ...target, quantities: { c2611: -99 } }, 'c2611').quantity).toBe(-20)
  })
  it('待执行、旧记录及换算不可用均不伪造目标手数', () => {
    for (const status of ['legacy', 'pending_execution', 'unavailable'] as const) {
      expect(holdingTarget({ ...target, sizing: { ...target.sizing!, status } }, 'c2611').quantity).toBeNull()
    }
    expect(holdingTarget(null, 'c2611').weight).toBeNull()
  })
  it('已计算目标中未包含的持仓需要清仓，非零权重量化为零仍保留权重', () => {
    expect(holdingTarget(target, 'ag2612')).toMatchObject({ quantity: 0, weight: 0 })
    const zero = { ...target, sizing: { ...target.sizing!, rows: { c2611: { ...evidence, target_quantity: 0 } } } }
    expect(holdingTarget(zero, 'c2611')).toMatchObject({ quantity: 0, weight: -0.6 })
  })
})

describe('实际持仓与数量差', () => {
  it('多空分开聚合，今昨仓不重复使用 net_position', () => {
    const both = holdingBook([...positions, { symbol: 'c2611', direction: '多头', volume: 3, available_volume: 2,
      extra: { long_td: 1, long_yd: 2, net_position: -15 } }], 'c2611', true)
    expect(both.long).toEqual({ volume: 3, available: 2, today: 1, yesterday: 2 })
    expect(both.short).toEqual({ volume: 18, available: 17, today: 3, yesterday: 15 })
    expect(holdingActions(both, -20)).toEqual([{ kind: '卖平', quantity: 3 }, { kind: '卖开', quantity: 2 }])
    expect(holdingActions(both, 5)).toEqual([{ kind: '买平', quantity: 18 }, { kind: '买开', quantity: 2 }])
  })
  it('缺数量、今昨仓分项时保持未知，空仓和未观测明确区分', () => {
    const unknown = holdingBook([{ symbol: 'c2611' }], 'c2611', true)
    expect(unknown.long.volume).toBeNull()
    expect(unknown.long.today).toBeNull()
    expect(holdingActions(unknown, 0)).toBeNull()
    expect(holdingBook([], 'c2611', false).long.volume).toBeNull()
    expect(holdingBook([], 'c2611', true).long.volume).toBe(0)
  })
})

describe('在途剩余数量核对', () => {
  it('部分成交只累计剩余1手，避免再计入已成交1手', () => {
    const result = project()
    expect(result.book?.short.volume).toBe(19)
    expect(result.actions).toEqual([{ kind: '卖开', quantity: 1 }])
    expect(result.effect).toBe('closer')
  })
  it('撤单已提交仍计入剩余数量，未知和提交中不提供确定结论', () => {
    expect(project({ receipts: [receipt] }).book?.short.volume).toBe(19)
    for (const state of ['submitting', 'unknown'] as const) expect(project({ receipts: [{ ...receipt, state }] }).book).toBeNull()
    expect(project({ receipts: [{ ...receipt, state: 'terminal' }] }).reason).toContain('尚未同步')
  })
  it('采样不同步、旧数据、查询失败与未知状态阻止确定预测', () => {
    expect(project({ observedAt: '2026-10-15T14:47:00' }).reason).toContain('采样时间不同')
    expect(project({ stale: true }).book).toBeNull()
    expect(project({ orders: null }).book).toBeNull()
    expect(project({ orders: { ...snapshot, complete: false } }).book).toBeNull()
    expect(project({ orders: { ...snapshot, data: [{ ...order, uncertain: true }] } }).book).toBeNull()
  })
  it('保留反向的平仓与开仓，检查未知开平和超量平仓', () => {
    const flip = project({ target: 3, orders: { ...snapshot, data: [
      { ...order, direction: 'BUY', offset: 'CLOSE', remaining_volume: 18 },
      { ...order, order_id: 'c-2', direction: 'BUY', offset: 'OPEN', remaining_volume: 3 },
    ] } })
    expect(flip.book?.long.volume).toBe(3)
    expect(flip.book?.short.volume).toBe(0)
    expect(flip.actions).toEqual([])
    expect(project({ orders: { ...snapshot, data: [{ ...order, offset: null }] } }).book).toBeNull()
    expect(project({ orders: { ...snapshot, data: [{ ...order, direction: 'BUY', offset: 'CLOSE', remaining_volume: 19 }] } }).book).toBeNull()
  })
  it('识别超过目标与扩大差额，零订单不会误报到位', () => {
    expect(project({ orders: { ...snapshot, data: [{ ...order, remaining_volume: 3 }] } }).effect).toBe('overshoot')
    expect(project({ orders: { ...snapshot, data: [{ ...order, direction: 'BUY', offset: 'CLOSE' }] } }).effect).toBe('farther')
    expect(project({ orders: { ...snapshot, data: [] } })).toMatchObject({ effect: 'unchanged', actions: [{ kind: '卖开', quantity: 2 }] })
  })
})
