import { expect, test } from 'bun:test'
import { tradesFromResult } from '@/features/account/journalActivity'

test('live expansion reads fills from the derived execution detail', () => {
  const trades = tradesFromResult({
      status: 'SUCCEEDED',
      symbol_results: {
        rb2610: {
          sizing: { unit_multiplier: 10 },
          first_tick: { bid_price: 99, ask_price: 101 },
          orders: [{ order_id: 'o1', direction: 'BUY' }],
          trades: [{ trade_price: 100, trade_volume: 1, order_id: 'o1' }],
        },
      },
  }, { id: 7, execution_id: 'exec-7', created_at: '2026-09-01T09:00:00' })
  expect(trades).toHaveLength(1)
  expect(trades[0]).toMatchObject({ symbol: 'rb2610', record_id: 7, execution_id: 'exec-7', value: 1000 })
})

test('missing execution summary yields no live fills', () => {
  expect(tradesFromResult(null, { created_at: '2026-09-01T09:00:00' })).toEqual([])
})
