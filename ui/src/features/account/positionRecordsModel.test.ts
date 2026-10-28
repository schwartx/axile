import { expect, test } from 'bun:test'
import { positionColumns, positionColumnText, positionCommonNumber, positionCurrencyUnit, positionDecimalText, positionDetails, positionText, positionValueLabel } from './positionRecordsModel'
import type { Position } from '@/types/api'

const external: Position = { extra: { position_attributes: {
  open_price: null, unrealized_pnl: 0, liquidation_price: null, mark_price: null, break_even_price: null,
  initial_margin: null, maintenance_margin: null, isolated_margin: null, isolated_wallet: null, adl: null,
} } }

test('channel columns stay fixed and use precise price and profit labels', () => {
  expect(positionColumns('ctp').map(column => column.label)).toEqual(['今仓 / 昨仓', '持仓均价', '持仓盈亏', '占用保证金', '开仓成本', '持仓成本', '冻结数量', '平仓盈亏', '手续费'])
  expect(positionColumns('tq').map(column => column.label)).toEqual(['今仓 / 昨仓', '开仓均价', '浮动盈亏', '持仓均价', '持仓盈亏', '占用保证金', '开仓成本', '持仓成本', '冻结数量'])
  expect(positionColumns('gm').map(column => column.label)).toEqual(['持仓均价', '浮动盈亏', '当前价格', '持仓成本', '持仓额', '今仓', '冻结数量', '摊薄均价', '摊薄口径盈亏', '开仓均价', '开仓口径盈亏'])
  expect(positionColumns('external', [external]).map(column => column.label)).toEqual(['开仓均价', '未实现盈亏', '强平价', '标记价', '保本价', '初始保证金', '维持保证金', '逐仓保证金', '逐仓钱包余额', '自动减仓排名'])
  expect(positionColumns('plugin')).toEqual([])
})

test('external columns use native key presence and share the union across sparse rows', () => {
  const positions: Position[] = [
    { extra: { position_attributes: { open_price: null, native_notional: null } } },
    { extra: { position_attributes: { unrealized_pnl: 0 } } },
  ]
  const columns = positionColumns('plugin', positions)
  expect(columns.map(column => column.key)).toEqual(['open_price', 'unrealized_pnl'])
  expect(positionColumnText(positions[0], columns[0], 'plugin')).toBe('—')
  expect(positionColumnText(positions[1], columns[1], 'plugin')).toBe('0.00')
  expect(positionValueLabel('plugin', positions)).toBe('名义价值')
  expect(positionValueLabel('plugin', [positions[1]])).toBe('市值')
  expect(positionValueLabel('gm', positions)).toBe('市值')
  expect(positionValueLabel('ctp', positions)).toBe('货值')
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

test('fiat units keep Chinese names and unknown currency codes remain explicit without conversion', () => {
  expect(positionCurrencyUnit(' cny ')).toBe('￥')
  expect(positionCurrencyUnit('CHF')).toBe('瑞郎')
  expect(positionCurrencyUnit('EUR')).toBe('欧元')
  expect(positionCurrencyUnit('USD')).toBe('美元')
  expect(positionCurrencyUnit('USDQ')).toBe('USDQ')
  expect(positionCurrencyUnit(' xau ')).toBe('XAU')
  expect(positionCurrencyUnit(null)).toBe('')
  expect(positionColumnText({ extra: { quote_asset: 'USDQ', position_attributes: { unrealized_pnl: 0 } } }, positionColumns('external', [external])[1], 'external')).toBe('0.00 USDQ')
})

test('legacy zero or reverse calculated averages do not appear as native prices', () => {
  for (const channel of ['ctp', 'gm', 'external']) {
    const column = positionColumns(channel, [external]).find(column => column.key.endsWith('price'))!
    for (const avg_price of [0, 65000]) expect(positionColumnText({ avg_price }, column, channel)).toBe('—')
  }
})

test('actual fields remain distinct, zero pnl survives and main columns are omitted from details', () => {
  const position: Position = { extra: { position_attributes: { open_price: 3200, position_price: 3100, float_profit: 0, position_profit: -10, margin: 12 } } }
  expect(positionColumnText(position, positionColumns('tq')[1], 'tq')).toBe('3,200.00')
  expect(positionColumnText(position, positionColumns('tq')[2], 'tq')).toBe('0.00')
  expect(positionDetails(position, 'tq').map(field => field.label)).toEqual([])
})

test('external cloned available and GM default zero market value are not exposed', () => {
  expect(positionCommonNumber({ volume: 2, available_volume: 2 }, 'available_volume', 'external')).toBeNull()
  expect(positionCommonNumber({ available_volume: 99, extra: { position_attributes: { available_now: 0 } } }, 'available_volume', 'external')).toBe(0)
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
  expect(positionCommonNumber({ market_value: 0 }, 'market_value', 'external')).toBeNull()
  expect(positionCommonNumber({ market_value: 125, extra: { position_attributes: { native_notional: null, market_value: 99 } } }, 'market_value', 'external')).toBeNull()
  expect(positionCommonNumber({ market_value: 0, extra: { position_attributes: { native_notional: 0 } } }, 'market_value', 'external')).toBe(0)
  expect(positionCommonNumber({ market_value: 125, extra: { position_attributes: { native_notional: -100 } } }, 'market_value', 'external')).toBe(125)
  const margin = positionColumns('external', [external]).find(column => column.key === 'initial_margin')!
  expect(positionColumnText({ extra: { quote_asset: 'USD', position_attributes: { initial_margin: 12 } } }, margin, 'external')).toBe('12.00')
  expect(positionColumnText({ extra: { position_attributes: { initial_margin: 12, margin_asset: 'EUR' } } }, margin, 'external')).toBe('12.00 欧元')
})
