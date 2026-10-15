import { expect, test } from 'bun:test'
import { canCancelOrder, mergeReceipts, orderOffset, orderState, orderSummary } from '@/features/account/currentOrdersModel'
import type { CancelReceipt, CurrentOrder, CurrentOrdersSnapshot } from '@/lib/api/orders'

const order: CurrentOrder = { order_id: 'o1', symbol: 'rb2610', direction: '买入', offset: '0', order_type: '限价', price: 3500,
  volume: 3, filled_volume: 1, remaining_volume: 2, status: '部分成交', create_time: null, execution_id: null, native_identity: {}, uncertain: false }
const snapshot: CurrentOrdersSnapshot = { data: [order], count: 1, complete: true, sampled_at: '2026-10-15T10:00:00', cancellations: [] }
const receipt: CancelReceipt = { request_id: 'r1', order_id: 'o1', symbol: 'rb2610', state: 'submitted', message: '等待回报', requested_at: '2026-10-15T10:01:00', order }

test('summary distinguishes zero orders from unknown and stale data', () => {
  expect(orderSummary(null, false)).toBe('当前委托 待确认')
  expect(orderSummary({ ...snapshot, data: [], count: 0 }, false)).toBe('当前委托 0 笔')
  expect(orderSummary({ ...snapshot, complete: false, count: null }, false)).toBe('当前委托 待确认')
  expect(orderSummary(snapshot, true)).toBe('当前委托 待确认')
})

test('submitted cancel keeps pending state and disables duplicate operations', () => {
  expect(orderState(order, receipt)).toBe('撤单确认中')
  expect(canCancelOrder(order, receipt, false)).toBe(false)
  expect(canCancelOrder(order, undefined, false)).toBe(true)
  expect(canCancelOrder(order, undefined, true)).toBe(false)
  expect(canCancelOrder({ ...order, uncertain: true }, undefined, false)).toBe(false)
  expect(orderState(order, { ...receipt, state: 'unknown' })).toBe('状态待确认')
})

test('actual terminal from refreshed server replaces accepted local receipt', () => {
  const terminal = { ...receipt, state: 'terminal' as const, order: { ...order, status: '已撤销' } }
  expect(mergeReceipts([terminal], [receipt])[0].state).toBe('terminal')
  expect(mergeReceipts([terminal], [{ ...receipt, state: 'unknown', requested_at: '2026-10-16T10:00:00Z' }])[0].state).toBe('terminal')
  const newer = { ...receipt, request_id: 'r2', requested_at: '2026-10-15T10:02:00' }
  expect(mergeReceipts([terminal], [newer])[0].request_id).toBe('r2')
})

test('channel close flags preserve today and yesterday distinctions', () => {
  expect(orderOffset('3')).toBe('平今')
  expect(orderOffset('4')).toBe('平昨')
  expect(orderOffset('CLOSETODAY')).toBe('平今')
  expect(orderOffset(null)).toBe('开平未知')
})
