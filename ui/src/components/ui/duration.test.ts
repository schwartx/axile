import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { DurationInput } from './DurationInput'
import { joinDuration, splitDuration, formatDuration, durationPartVisible } from './duration'

test('零单位在展示时隐藏，编辑展开全部分段，空值和全零仍可操作', () => {
  const parts = splitDuration('90', true)
  expect(['hours', 'minutes', 'seconds'].map(key => durationPartVisible(parts, key as keyof typeof parts)))
    .toEqual([false, true, true])
  expect(durationPartVisible(parts, 'hours', true)).toBe(true)
  expect(durationPartVisible(splitDuration('3600', true), 'minutes')).toBe(false)
  expect(durationPartVisible(splitDuration('3600', true), 'seconds')).toBe(false)
  expect(durationPartVisible(splitDuration('0', true), 'seconds')).toBe(true)
  expect(durationPartVisible({ ...parts, hours: '' }, 'hours')).toBe(true)
})

test('分秒和时分秒均精确往返，分钟可超过 59，输入溢出在结束编辑后归一', () => {
  for (const value of ['0', '1', '59', '60', '210', '999', '3599', '3600', '86400', '9007199254740991']) {
    for (const hours of [false, true]) expect(joinDuration(splitDuration(value, hours))).toBe(value)
  }
  expect(splitDuration('210', false)).toEqual({ hours: '0', minutes: '3', seconds: '30' })
  expect(splitDuration('4830', true)).toEqual({ hours: '1', minutes: '20', seconds: '30' })
  expect(joinDuration({ hours: '0', minutes: '3', seconds: '90' })).toBe('270')
})

test('空段、非法输入、小数秒、负数及不安全整数不能变成合法零秒', () => {
  for (const seconds of ['', ' ', 'abc', '1.5', '-1', '1e3', 'Infinity', '9007199254740992'])
    expect(joinDuration({ hours: '0', minutes: '3', seconds })).toBe('NaN')
  expect(joinDuration({ hours: '0', minutes: '', seconds: '30' })).toBe('NaN')
  expect(splitDuration('', true).seconds).toBe('')
})

test('摘要省略零单位，保留整数秒精度', () => {
  expect([0, 1, 60, 210, 1000, 3600, 4830, 86400].map(formatDuration))
    .toEqual(['0 秒', '1 秒', '1 分', '3 分 30 秒', '16 分 40 秒', '1 时', '1 时 20 分 30 秒', '24 时'])
})

test('紧凑输入按范围固定分段，保留真实输入、帮助关联、边界禁用与 NumberFlow', () => {
  const render = (extra = {}) => renderToStaticMarkup(createElement(DurationInput, {
    value: '210', onChange() {}, step: 30, min: 1, max: 540, size: 'sm', ariaLabel: '执行超时',
    id: 'timeout', describedBy: 'help', ...extra,
  }))
  const html = render()
  expect(html).toContain('number-flow-react')
  expect(html).toContain('aria-label="执行超时：分"')
  expect(html).toContain('aria-label="执行超时：秒"')
  expect(html).not.toContain('aria-label="执行超时：时"')
  expect(html).toContain('id="timeout"')
  expect(html).toContain('aria-describedby="help"')
  expect(html).toContain('value="3"')
  expect(html).toContain('value="30"')
  expect(render({ max: 86400 })).toContain('aria-label="执行超时：时"')
  expect(render({ value: 'NaN', invalid: true })).toContain('aria-invalid="true"')
  expect((render({ value: '540' }).match(/disabled=""/g) ?? [])).toHaveLength(1)
  expect((render({ disabled: true }).match(/disabled=""/g) ?? [])).toHaveLength(4)
})
