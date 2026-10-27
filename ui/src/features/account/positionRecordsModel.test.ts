import { expect, test } from 'bun:test'
import { positionColumns, positionColumnText, positionCommonNumber, positionCurrencyUnit, positionDecimalText, positionDetails, positionText } from './positionRecordsModel'
import type { Position } from '@/types/api'

test('channel columns stay fixed and use precise price and profit labels', () => {
  expect(positionColumns('ctp').map(column => column.label)).toEqual(['今仓 / 昨仓', '持仓均价', '持仓盈亏', '占用保证金', '开仓成本', '持仓成本', '冻结数量', '平仓盈亏', '手续费'])
  expect(positionColumns('tq').map(column => column.label)).toEqual(['今仓 / 昨仓', '开仓均价', '浮动盈亏', '持仓均价', '持仓盈亏', '占用保证金', '开仓成本', '持仓成本', '冻结数量'])
  expect(positionColumns('gm').map(column => column.label)).toEqual(['持仓均价', '浮动盈亏', '当前价格', '持仓成本', '持仓额', '今仓', '冻结数量', '摊薄均价', '摊薄口径盈亏', '开仓均价', '开仓口径盈亏'])
  expect(positionColumns('plugin')).toEqual([])
})

test('missing means dash, explicit zero survives and tiny values never round to zero', () => {
  for (const value of [undefined, null, NaN, Infinity, true, '0']) expect(positionText(value)).toBe('—')
  expect(positionText(0)).toBe('0')
  expect(positionDecimalText(2201.68173115362)).toBe('2,201.68')
  expect(positionDecimalText(13450.846430606209)).toBe('13,450.85')
  expect(positionDecimalText(-180)).toBe('-180.00')
  expect(positionDecimalText(undefined)).toBe('—')
  expect(positionText(1e-15)).toBe('1e-15')
})

test('currency units are Chinese names without converting the actual amount', () => {
  expect(positionCurrencyUnit(' cny ')).toBe('元')
  expect(positionCurrencyUnit('USD')).toBe('美元')
  expect(positionCurrencyUnit(' usdq ')).toBe('USDQ')
  expect(positionCurrencyUnit(null)).toBe('')
})

test('legacy zero or reverse calculated averages do not appear as native prices', () => {
  for (const channel of ['ctp', 'gm', 'tq']) {
    const column = positionColumns(channel).find(column => column.key.endsWith('price'))!
    for (const avg_price of [0, 65000]) expect(positionColumnText({ avg_price }, column, channel)).toBe('—')
  }
})

test('unknown channels keep common values and native details without fixed columns', () => {
  const position: Position = { volume: 2, available_volume: 0, market_value: 0, extra: { position_attributes: { open_price: 3200, unrealized_pnl: 0 } } }
  expect(positionColumns('plugin')).toEqual([])
  expect(positionCommonNumber(position, 'available_volume', 'plugin')).toBe(0)
  expect(positionCommonNumber(position, 'market_value', 'plugin')).toBe(0)
  expect(positionDetails(position, 'plugin')).toEqual([
    { key: 'open_price', label: '开仓均价', text: '3,200.00' },
    { key: 'unrealized_pnl', label: '未实现盈亏', text: '0.00' },
  ])
})

test('actual fields remain distinct, zero pnl survives and main columns are omitted from details', () => {
  const position: Position = { extra: { position_attributes: { open_price: 3200, position_price: 3100, float_profit: 0, position_profit: -10, margin: 12 } } }
  expect(positionColumnText(position, positionColumns('tq')[1], 'tq')).toBe('3,200.00')
  expect(positionColumnText(position, positionColumns('tq')[2], 'tq')).toBe('0.00')
  expect(positionDetails(position, 'tq').map(field => field.label)).toEqual([])
})

test('missing native availability and GM default zero market value are not exposed', () => {
  expect(positionCommonNumber({ market_value: 0 }, 'market_value', 'gm')).toBeNull()
  expect(positionCommonNumber({ extra: { position_attributes: { available_now: 0, available: 12 } } }, 'available_volume', 'gm')).toBe(0)
  expect(positionCommonNumber({ extra: { position_attributes: { available: 12 } } }, 'available_volume', 'gm')).toBeNull()
  expect(positionCommonNumber({ available_volume: 3 }, 'available_volume', 'tq')).toBeNull()
})

test('CTP yesterday means remaining yesterday holdings, not YdPosition', () => {
  const position = { direction: '多头', extra: { long_yd: 2, position_attributes: { today_volume: 1, previous_volume: 5 } } }
  expect(positionColumnText(position, positionColumns('ctp')[0], 'ctp')).toBe('1 / 2')
})

test('promoted quantities retain precision and missing native values never borrow other fields', () => {
  const frozen = positionColumns('ctp').find(column => column.key === 'frozen')!
  expect(positionColumnText({ direction: '空头', extra: { frozen: 99, position_attributes: { short_frozen: 0, long_frozen: 9 } } }, frozen, 'ctp')).toBe('0')
  expect(positionColumnText({ direction: '空头', extra: { frozen: 99 } }, frozen, 'ctp')).toBe('—')
  const gmFrozen = positionColumns('gm').find(column => column.key === 'frozen')!
  expect(positionColumnText({ extra: { position_attributes: { frozen: 0.123456 } } }, gmFrozen, 'gm')).toBe('0.123456')
})
