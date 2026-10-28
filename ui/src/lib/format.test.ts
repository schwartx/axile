import { expect, test } from 'bun:test'

import { bpsCls, displayCurrencyUnit, fmtBps, withCurrency } from './format'

test('人民币币种代码显示为￥', () => {
  expect(displayCurrencyUnit('CNY')).toBe('￥')
  expect(displayCurrencyUnit(' cny ')).toBe('￥')
  expect(displayCurrencyUnit('USDQ')).toBe('USDQ')
  expect(displayCurrencyUnit(null)).toBe('')
})

test('withCurrency preserves public and plugin currency formatting', () => {
  expect(withCurrency('100.00', 'CNY')).toBe('￥100.00')
  expect(withCurrency('100.00', 'USDQ')).toBe('100.00 USDQ')
  expect(withCurrency('100.00', 'EUR')).toBe('100.00 EUR')
})

test('fmtBps 带号两位小数，零为正号', () => {
  expect(fmtBps(1.234)).toBe('+1.23bps')
  expect(fmtBps(-0.5)).toBe('−0.50bps')
  expect(fmtBps(0)).toBe('+0.00bps')
})

test('bpsCls 滑点三态：有利 accent、不利 warn、持平中性', () => {
  expect(bpsCls(1)).toBe('text-accent')
  expect(bpsCls(0.06)).toBe('text-accent')
  expect(bpsCls(-1)).toBe('text-warn')
  expect(bpsCls(-0.06)).toBe('text-warn')
  expect(bpsCls(0.05)).toBe('text-ink-3')
  expect(bpsCls(-0.05)).toBe('text-ink-3')
  expect(bpsCls(0)).toBe('text-ink-3')
})
