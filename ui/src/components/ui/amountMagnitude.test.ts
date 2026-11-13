import { expect, test } from 'bun:test'
import { amountMagnitude } from '@/components/ui/amountMagnitude'

test('金额按百、千、万、亿提示，量级边界不舍入', () => {
  for (const [value, hint] of [
    ['0', '0'], ['99.99', '99.99'], ['100', '1百'], ['999.99', '9.9999百'],
    ['1000', '1千'], ['9999.99', '9.99999千'], ['10000', '1万'],
    ['125000', '12.5万'], ['99999999.99', '9999.999999万'], ['100000000', '1亿'],
    ['0001000.0500', '1.00005千'], ['.0050', '0.005'],
    ['100000000.00000001', '1.0000000000000001亿'],
  ]) expect(amountMagnitude(value)).toBe(hint)
})

test('空草稿、未完成小数、负数、科学计数及非有限金额没有提示', () => {
  for (const value of ['', ' ', '1000\n', '.', '1.', '-1', '+1000', '1e3', '1,000', '12元', 'NaN', 'Infinity', '9'.repeat(309)])
    expect(amountMagnitude(value)).toBe('')
})
