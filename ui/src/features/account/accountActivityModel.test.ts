import { expect, test } from 'bun:test'
import { endedActivityOrders, operationEvents } from '@/features/account/accountActivityModel'
import type { AccountActivity, CurrentOrder } from '@/lib/api/orders'

const order: CurrentOrder = { order_id: 'one', symbol: 'rb2610', direction: 'BUY', offset: 'OPEN', order_type: 'LIMIT', price: 100, volume: 2, filled_volume: 1, remaining_volume: 1, status: '部分成交', create_time: '2026-10-16T09:00:00', execution_id: null, native_identity: {}, uncertain: false }
const activity: AccountActivity = { orders: [order], trades: [], failures: [], scope: '当前交易日', started_at: '2026-10-16T09:00:00', sampled_at: '2026-10-16T09:00:00', queried_at: null, errors: [], symbols: [], limit: 100, truncated: false }

test('cancel rejection is an independent event and does not turn an active order into a canceled one', () => {
  const events = operationEvents({ ...activity, failures: [{ event_id: 'r1', operation: 'cancel_order', outcome: 'rejected', symbol: order.symbol, order_id: order.order_id, message: '柜台不允许撤单', time: '2026-10-16T10:00:00' }] })
  expect(events).toHaveLength(1)
  expect(events[0].label).toBe('撤单被拒绝')
  expect(activity.orders[0].status).toBe('部分成交')
  expect(endedActivityOrders(activity)).toHaveLength(0)
})

test('terminal orders from different symbols keep separate identities and unknown results stay unknown', () => {
  const events = operationEvents({ ...activity, orders: [{ ...order, status: '已撤销' }, { ...order, symbol: 'ag2612', status: '已拒绝', reason: '保证金不足' }], failures: [{ event_id: 'r1', operation: 'place_order', symbol: 'cu2612', order_id: null, message: '结果待确认', time: '2026-10-16T10:00:00' }] })
  expect(events).toHaveLength(1)
  expect(events[0].label).toBe('下单失败 / 待确认')
  const ended = endedActivityOrders({ ...activity, orders: [{ ...order, status: '已撤销' }, { ...order, symbol: 'ag2612', status: '已拒绝', reason: '保证金不足' }, { ...order, symbol: 'expired', status: '已过期' }, order] })
  expect(ended).toHaveLength(3)
  expect(ended.find(event => event.symbol === 'ag2612')?.reason).toBe('保证金不足')
  expect(events[0].operation).toBe('下单')
})
