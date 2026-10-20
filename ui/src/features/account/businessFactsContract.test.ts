import { expect, test } from 'bun:test'
import { executionRecordError } from '@/features/account/executionRecordError'
import { executionRecordSummary, executionRecordView } from '@/features/account/executionOutcome'
import { recordOrders } from '@/features/account/recordOrders'

test('compact business head supplies outcome and counts without expanding detail', () => {
  const view = executionRecordView({
    id: 2701, execution_id: 'facts-2701', created_at: '2026-10-20T10:00:00', is_success: 0,
    status: 'PARTIAL', symbol_count: 12, trade_count: 7,
    symbol_results: { rb2701: { status: 'PARTIAL', error: '柜台原始原因' } },
  })
  expect(view).toMatchObject({ state: 'PARTIAL', symbolCount: 12, tradeCount: 7, reason: '柜台原始原因' })
  expect(executionRecordSummary({ status: 'SUCCEEDED', symbol_count: 12, trade_count: 7 })).toBe('调仓 · 涉及 12 个品种 · 7 笔成交')
})

test('explicit zero counts and null head state mask stale detail fields', () => {
  const view = executionRecordView({
    status: null, task_status: null, error: '', execution_kind: null, reason_code: '',
    symbol_count: 0, trade_count: 0, is_success: 1,
    result: {
      status: 'SUCCEEDED', task_status: 'TERMINATED', error: '过期错误', execution_kind: 'clear_positions',
      symbol_results: { rb2701: { status: 'SUCCEEDED', trades: [{ trade_volume: 1 }] } },
    },
  })
  expect(view).toMatchObject({ state: 'UNKNOWN', symbolCount: 0, tradeCount: 0, reason: '' })
  expect(view.title).toBe('执行状态未知')
  expect(executionRecordError({ error: '', result: { error: '过期错误' } })).toBe('')
  expect(executionRecordError({ error: null, result: { error: '过期错误' } })).toBe('')
})

test('absent state stays unknown while typed detail exposes its own result state', () => {
  for (const is_success of [0, 1]) {
    expect(executionRecordView({ is_success, symbol_count: 0, trade_count: 0 }).state).toBe('UNKNOWN')
  }
  expect(executionRecordView({ result: { status: 'NOOP', execution_kind: 'clear_positions' } }).title).toBe('无需清仓')
  expect(executionRecordError({ result: { interrupt_reason: 'process_interrupted' } })).toBe('上次执行中断，未自动续跑')
})

test('order evidence retains unknown, zero and rebate fees independently', () => {
  const [order] = recordOrders({
    orders: [{ order_id: 'native-order', price: 0, volume: 0, filled_volume: null }],
    trades: [
      { order_id: 'native-order', trade_price: 0, trade_volume: null },
      { order_id: 'native-order', extra: { commission: null, commission_asset: 'CNY' } },
      { order_id: 'native-order', extra: { commission: 0, commission_asset: 'CNY' } },
      { order_id: 'native-order', extra: { commission: -0.125, commission_asset: 'USD' } },
    ],
  })
  expect(order).toMatchObject({ price: 0, volume: 0, filled_volume: null })
  expect(order.trades.map(trade => trade.fee)).toEqual([null, null, 0, -0.125])
  expect(order.trades.map(trade => trade.fee_asset)).toEqual([null, 'CNY', 'CNY', 'USD'])
  expect(order.trades[0]).toMatchObject({ price: 0, volume: null, time: null })
})
