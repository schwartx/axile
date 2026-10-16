import { expect, test } from 'bun:test'
import { activityPositionKey, activityView, rememberActivityView } from './activityViewMemory'

test('view memory isolates accounts and keeps a scroll position for each tab and symbol', () => {
  const first = activityView(901)
  const key = activityPositionKey('trades', 'XRPUSDT')
  first.positions.set(key, { top: 640, left: 210 })
  rememberActivityView(901, { ...first, tab: 'trades', symbol: 'XRPUSDT', limit: 200 })
  expect(activityView(901)).toMatchObject({ tab: 'trades', symbol: 'XRPUSDT', limit: 200 })
  expect(activityView(901).positions.get(key)).toEqual({ top: 640, left: 210 })
  expect(activityView(902)).toMatchObject({ tab: 'pending', symbol: '', operationSymbol: '', limit: 100, operationLimit: 100 })
  expect(activityView(902).positions.size).toBe(0)
  rememberActivityView(901, { ...activityView(901), tab: 'operations', operationSymbol: 'BTCUSDT', operationLimit: 300 })
  expect(activityView(901)).toMatchObject({ symbol: 'XRPUSDT', limit: 200, operationSymbol: 'BTCUSDT', operationLimit: 300 })
  expect(activityPositionKey('operations', 'XRPUSDT')).not.toBe(key)
  expect(activityPositionKey('orders', 'XRPUSDT')).not.toBe(key)
  expect(activityPositionKey('trades', 'BTCUSDT')).not.toBe(key)
  expect(activityPositionKey('pending', 'BTCUSDT')).toBe(activityPositionKey('pending', ''))
})
