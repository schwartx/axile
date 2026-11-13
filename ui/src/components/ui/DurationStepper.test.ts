import { expect, test } from 'bun:test'
import { joinHourDuration, splitHourDuration } from '@/components/ui/duration'

test('总小时数精确往返天小时，溢出的小时在结束编辑时进位', () => {
  for (const value of ['0', '1', '23', '24', '26', '8760', '9007199254740991'])
    expect(joinHourDuration(splitHourDuration(value))).toBe(value)
  expect(splitHourDuration('26')).toEqual({ days: '1', hours: '2' })
  expect(splitHourDuration(joinHourDuration({ days: '1', hours: '24' }))).toEqual({ days: '2', hours: '0' })
})

test('空段、负数、小数和不安全整数保留为非法草稿，不能变成零小时', () => {
  for (const value of ['', ' ', '-1', '1.5', 'invalid', 'Infinity', '9007199254740992']) {
    expect(joinHourDuration({ days: value, hours: '2' })).toBe('NaN')
    expect(joinHourDuration({ days: '1', hours: value })).toBe('NaN')
  }
  expect(joinHourDuration({ days: '9007199254740991', hours: '0' })).toBe('NaN')
  expect(splitHourDuration('').hours).toBe('')
})
