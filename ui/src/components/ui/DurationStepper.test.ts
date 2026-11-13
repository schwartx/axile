import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { DurationStepper } from './DurationStepper'
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

test('省略零天或零小时，全零保留小时入口，非法值保持可编辑', () => {
  for (const [value, hidden] of [['2', ['days']], ['24', ['hours']], ['26', []], ['0', ['days']], ['', []], ['NaN', []]] as const) {
    const html = renderToStaticMarkup(createElement(DurationStepper, { value, onChange() {}, ariaLabel: '间隔' }))
    for (const key of ['days', 'hours'])
      expect(html).toContain(`data-duration-unit="${key}"${hidden.some(unit => unit === key) ? ' inert=""' : ''} aria-hidden="${hidden.some(unit => unit === key)}"`)
  }
})

test('按天保留复用四个步进按钮和两种动效，空值合法且只能向上步进', () => {
  const render = (value: string) => renderToStaticMarkup(createElement(DurationStepper, {
    value, unit: 'days', min: 1, emptyLabel: '不清理', onChange() {}, ariaLabel: '保留天数',
  }))
  const empty = render('')
  expect(empty).toContain('不清理')
  expect(empty).toContain('ink-rewrite-slot')
  expect(empty).toContain('number-flow-react')
  expect(empty).not.toContain('aria-invalid="true"')
  expect(empty).not.toContain('data-duration-unit="hours"')
  expect((empty.match(/disabled=""/g) ?? [])).toHaveLength(2)
  for (const amount of [1, 7]) {
    expect(empty).toContain(`aria-label="保留天数：增加 ${amount} 天"`)
    expect(empty).toContain(`aria-label="保留天数：减少 ${amount} 天"`)
  }
  expect((render('1').match(/disabled=""/g) ?? [])).toHaveLength(0)
  for (const value of ['0', '-1', '1.5', 'invalid', '9007199254740992'])
    expect(render(value)).toContain('aria-invalid="true"')
})
