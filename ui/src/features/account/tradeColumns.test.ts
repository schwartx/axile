import { expect, test } from 'bun:test'
import { tradeColumns } from '@/features/account/tradeColumns'
import type { AccountTrade } from '@/lib/api/orders'

const trade: AccountTrade = { trade_id: 'one', order_id: 'order', symbol: 'symbol', direction: 'BUY', offset: 'OPEN', time: '', price: 100, volume: 1, value: 100 }
const cells = (channel: string, fields: Partial<AccountTrade>) => Object.fromEntries(tradeColumns(channel).map(column => [column.key, column.text({ ...trade, ...fields })]))

test('CTP trading day and hedge semantics and TQ exchange trade ID are explicit columns', () => {
  expect(cells('ctp', { trading_day: '20261019', hedge_flag: '3' })).toEqual({ offset: '开仓', trading_day: '2026-10-19', hedge_flag: '套保' })
  expect(cells('tq', { offset: 'CLOSETODAY', exchange_trade_id: 'exchange-one' })).toEqual({ offset: '平今', exchange_trade_id: 'exchange-one' })
})

test('GM uses native amount and distinguishes a zero commission from absent costs', () => {
  expect(cells('gm', { amount: 7837.4997377, commission: 0, cost: null })).toEqual({ amount: '7,837.50', commission: '0.00', cost: '—' })
})

test('Binance distinguishes Taker from unknown and preserves small fees and true PnL coloring', () => {
  expect(cells('binance', { commission: 0.000000001, commission_asset: 'BNB', realized_pnl: -2.5, is_maker: false, position_side: 'SHORT' })).toEqual({ commission: '0.000000001', commission_asset: 'BNB', realized_pnl: '-2.5', is_maker: 'Taker', position_side: '空头' })
  expect(cells('binance', {}).is_maker).toBe('—')
  const pnl = tradeColumns('binance').find(column => column.key === 'realized_pnl')!
  expect(pnl.color?.({ ...trade, realized_pnl: -2.5 })).toBe('text-down')
  expect(pnl.color?.({ ...trade, realized_pnl: 0 })).toBe('')
})
