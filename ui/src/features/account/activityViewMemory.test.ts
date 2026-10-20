import { expect, test } from 'bun:test'
import { activityPositionKey, activityView, rememberActivityView } from './activityViewMemory'

test('view memory isolates accounts and keeps a scroll position for each tab and symbol', () => {
  const first = activityView(901)
  const key = activityPositionKey('trades', 'SHFE.ag2612')
  first.positions.set(key, { top: 640, left: 210 })
  rememberActivityView(901, { ...first, tab: 'trades', symbol: 'SHFE.ag2612', limit: 200 })
  expect(activityView(901)).toMatchObject({ tab: 'trades', symbol: 'SHFE.ag2612', limit: 200 })
  expect(activityView(901).positions.get(key)).toEqual({ top: 640, left: 210 })
  expect(activityView(902)).toMatchObject({ tab: 'pending', symbol: '', operationSymbol: '', limit: 100, operationLimit: 100 })
  expect(activityView(902).positions.size).toBe(0)
  rememberActivityView(901, { ...activityView(901), tab: 'operations', operationSymbol: 'SHFE.rb2610', operationLimit: 300 })
  expect(activityView(901)).toMatchObject({ symbol: 'SHFE.ag2612', limit: 200, operationSymbol: 'SHFE.rb2610', operationLimit: 300 })
  expect(activityPositionKey('operations', 'SHFE.ag2612')).not.toBe(key)
  expect(activityPositionKey('orders', 'SHFE.ag2612')).not.toBe(key)
  expect(activityPositionKey('trades', 'SHFE.rb2610')).not.toBe(key)
  expect(activityPositionKey('pending', 'SHFE.rb2610')).toBe(activityPositionKey('pending', ''))
  rememberActivityView(901, { ...activityView(901), tab: 'positions', positionSymbol: 'rb2610' })
  expect(activityView(901)).toMatchObject({ positionSymbol: 'rb2610', symbol: 'SHFE.ag2612', operationSymbol: 'SHFE.rb2610' })
  expect(activityPositionKey('positions', 'rb2610')).not.toBe(activityPositionKey('trades', 'rb2610'))
})
