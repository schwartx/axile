import { expect, test } from 'bun:test'
import { tradeDetails } from '@/features/account/tradeDetails'
import type { AccountTrade } from '@/lib/api/orders'
const trade: AccountTrade = { trade_id: 'one', order_id: 'order', symbol: 'symbol', direction: 'BUY', offset: 'OPEN', time: '', price: 100, volume: 1, value: 100 }

test('trade details omit absent fields and duplicate native order IDs', () => {
  expect(tradeDetails(trade)).toEqual([])
  expect(tradeDetails({ ...trade, attributes: { cl_ord_id: 'order', exchange_order_id: 'counter', gm_symbol: 'SHSE.600000', password: 'secret' } }, 'gm'))
    .toEqual([{ key: 'gm_symbol', label: '原生证券代码', text: 'SHSE.600000', copyable: false },
      { key: 'exchange_order_id', label: '柜台委托编号', text: 'counter', copyable: true },
      { key: 'offset', label: '开平', text: '开仓', copyable: false }])
})

test('trade details retain raw CTP calendar date and expose copyable TQ exchange ID', () => {
  const fields = tradeDetails({ ...trade, attributes: { event_date: '20261016', event_clock: '21:01:02', exchange_id: 'SHFE', order_ref: 'ref-1' }, trading_day: '20261019' }, 'ctp')
  expect(fields.find(field => field.key === 'event_date')?.text).toBe('2026-10-16')
  expect(fields.find(field => field.key === 'order_ref')?.copyable).toBe(true)
  expect(fields.some(field => field.key === 'trading_day')).toBe(false)
  expect(tradeDetails({ ...trade, exchange_trade_id: 'exchange-one' }, 'tq')).toEqual([{ key: 'exchange_trade_id', label: '交易所成交编号', text: 'exchange-one', copyable: true }])
})

test('trade event status describes the fill event and unknown enum values stay readable', () => {
  const fields = tradeDetails({ ...trade, attributes: { execution_type: 'TRADE', order_status: 'PARTIALLY_FILLED', client_order_id: 'client' } }, 'external')
  expect(fields.find(field => field.key === 'order_status')?.text).toBe('部分成交')
  expect(fields.find(field => field.key === 'execution_type')?.text).toBe('成交')
  expect(tradeDetails({ ...trade, attributes: { execution_type: 'CUSTOM' } })[0].text).toBe('CUSTOM')
})
