import { expect, test } from 'bun:test'
import { canCancelOrder, currentOrderKey, mergeReceipts, orderOffset, orderState, orderSummary, ordersSnapshotLabel, reconcileLocalReceipts, updateLocalReceipt } from '@/features/account/currentOrdersModel'
import type { CancelReceipt, CurrentOrder, CurrentOrdersSnapshot } from '@/lib/api/orders'

const order: CurrentOrder = { order_id: 'o1', symbol: 'rb2610', direction: '买入', offset: '0', order_type: '限价', price: 3500,
  volume: 3, filled_volume: 1, remaining_volume: 2, status: '部分成交', create_time: null, execution_id: null, native_identity: {}, uncertain: false }
const snapshot: CurrentOrdersSnapshot = { data: [order], count: 1, complete: true, sampled_at: '2026-10-15T10:00:00', cancellations: [] }
const receipt: CancelReceipt = { request_id: 'r1', order_id: 'o1', symbol: 'rb2610', state: 'submitted', message: '等待回报', requested_at: '2026-10-15T10:01:00', order }
const terminal: CancelReceipt = { ...receipt, state: 'terminal', message: '订单实际状态：已撤销', order: { ...order, status: '已撤销' } }

test('automatic summary distinguishes zero orders from unknown and stale data', () => {
  expect(orderSummary(snapshot, false)).toBe('在途委托 · 1')
  expect(orderSummary(null, false)).toBe('在途委托')
  expect(orderSummary({ ...snapshot, data: [], count: 0 }, false)).toBe('在途委托 · 0')
  expect(orderSummary({ ...snapshot, complete: false, count: null }, false)).toBe('在途委托')
  expect(orderSummary(snapshot, true)).toBe('在途委托 · 1')
})

test('submitted cancel keeps pending state and disables duplicate operations', () => {
  expect(orderState(order, receipt)).toBe('撤单确认中')
  expect(canCancelOrder(order, receipt, false)).toBe(false)
  expect(canCancelOrder(order, undefined, false)).toBe(true)
  expect(canCancelOrder(order, undefined, true)).toBe(false)
  expect(canCancelOrder({ ...order, uncertain: true }, undefined, false)).toBe(false)
  expect(orderState(order, { ...receipt, state: 'unknown' })).toBe('待确认')
})

test('actual terminal from refreshed server replaces accepted local receipt', () => {
  expect(mergeReceipts([terminal], [receipt])[0].state).toBe('terminal')
  expect(mergeReceipts([terminal], [{ ...receipt, state: 'unknown', requested_at: '2026-10-16T10:00:00Z' }])[0].state).toBe('terminal')
  const newer = { ...receipt, request_id: 'r2', requested_at: '2026-10-15T10:02:00' }
  expect(mergeReceipts([terminal], [newer])).toEqual([terminal])
})

test.each(['submitting', 'submitted', 'unknown', 'failed'] as const)('order terminal overrides another request in %s regardless of client clock', state => {
  const newer = { ...receipt, request_id: 'r2', state, requested_at: '2099-10-15T10:02:00Z' }
  expect(mergeReceipts([terminal], [newer])).toEqual([terminal])
  expect(canCancelOrder(order, mergeReceipts([terminal], [newer])[0], false)).toBe(false)
})

test.each(['submitting', 'submitted', 'unknown', 'failed'] as const)('terminal cannot regress to a late %s receipt from the same or another request', state => {
  for (const request_id of ['r1', 'r2']) {
    const late = { ...receipt, request_id, state, requested_at: '2099-10-15T10:02:00Z' }
    expect(mergeReceipts([late], [terminal])).toEqual([terminal])
    expect(mergeReceipts([terminal, late], [])).toEqual([terminal])
    expect(mergeReceipts([late, terminal], [])).toEqual([terminal])
    expect(updateLocalReceipt([terminal], late)).toEqual([terminal])
  }
})

test.each(['已撤销', '已成交', '已拒绝'])('terminal shows actual order status %s over an uncertain snapshot', status => {
  const actual = { ...terminal, order: { ...order, status, filled_volume: status === '已成交' ? 3 : 1 } }
  expect(orderState({ ...order, uncertain: true }, actual)).toBe(status)
  expect(mergeReceipts([actual], [{ ...receipt, request_id: 'r2', state: 'unknown' }])[0].order).toEqual(actual.order)
})

test('nonterminal receipts from another request cannot resolve a local unknown result', () => {
  const unknown = { ...receipt, request_id: 'r2', state: 'unknown' as const }
  for (const state of ['submitted', 'unknown', 'failed'] as const) {
    expect(mergeReceipts([{ ...receipt, state, requested_at: '2099-10-15T10:02:00Z' }], [unknown])).toEqual([unknown])
  }
  const failed = { ...receipt, state: 'failed' as const }
  expect(mergeReceipts([failed], [{ ...receipt, state: 'unknown' }])).toEqual([failed])
  expect(canCancelOrder(order, failed, false)).toBe(true)
})

test('failed request cannot regress but a new manual request replaces it without comparing clocks', () => {
  const failed = { ...receipt, state: 'failed' as const, requested_at: '2099-10-15T10:02:00Z' }
  expect(updateLocalReceipt([failed], { ...receipt, state: 'unknown' })).toEqual([failed])
  expect(mergeReceipts([receipt], [failed])).toEqual([failed])
  const retry = { ...receipt, request_id: 'r2', state: 'submitting' as const }
  expect(updateLocalReceipt([failed], retry)).toEqual([retry])
})

test('query reconciliation remembers terminal after server history drops it and ignores unrelated orders', () => {
  const unknown = { ...receipt, request_id: 'r2', state: 'unknown' as const }
  const unrelated = { ...receipt, order_id: 'other-order' }
  const reconciled = reconcileLocalReceipts([terminal, unrelated], [unknown])
  expect(reconciled).toEqual([terminal])
  expect(reconcileLocalReceipts([], reconciled)).toEqual([terminal])
  expect(mergeReceipts([], reconciled)).toEqual([terminal])
  expect(updateLocalReceipt(reconciled, unknown)).toEqual([terminal])
  expect(reconcileLocalReceipts([], [unknown])).toEqual([unknown])
})

test('all receipt paths distinguish symbols sharing an order ID and preserve the other local operation', () => {
  const unknown = { ...receipt, state: 'unknown' as const }
  const otherSymbol = { ...terminal, symbol: 'ag2612', order: { ...terminal.order!, symbol: 'ag2612' } }
  expect(mergeReceipts([otherSymbol], [unknown])).toEqual([unknown, otherSymbol])
  expect(reconcileLocalReceipts([otherSymbol], [unknown])).toEqual([unknown])
  expect(updateLocalReceipt([unknown], otherSymbol)).toEqual([unknown, otherSymbol])
  expect(currentOrderKey({ symbol: 'a:b', order_id: 'c' })).not.toBe(currentOrderKey({ symbol: 'a', order_id: 'b:c' }))
})

test('channel close flags preserve today and yesterday distinctions', () => {
  expect(orderOffset('3')).toBe('平今')
  expect(orderOffset('4')).toBe('平昨')
  expect(orderOffset('CLOSETODAY')).toBe('平今')
  expect(orderOffset(null)).toBe('开平未知')
})

test('未采样时间未知，失联订单保留原采样时间与独立状态', () => {
  expect(ordersSnapshotLabel(null, false, 0)).toBe('— 笔 · — 更新')
  expect(ordersSnapshotLabel({ ...snapshot, data: [], count: null, complete: false }, false, 0)).toBe('— 笔 · — 更新')
  expect(ordersSnapshotLabel({ ...snapshot, count: null, complete: false }, false, 1)).toBe('— 笔 · 2026-10-15 10:00:00 更新')
  expect(ordersSnapshotLabel({ ...snapshot, data: [], count: 0, complete: false, version: 2 }, false, 0)).toContain('2026-10-15 10:00:00')
  expect(orderState(order)).toBe('部分成交')
  expect(orderState(order, { ...receipt, state: 'failed' })).toBe('撤单失败')
})
