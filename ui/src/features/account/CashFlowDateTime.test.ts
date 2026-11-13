import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { CashFlowDateTime } from '@/features/account/CashFlowDateTime'
import { cashFlowTimeError, cashFlowTimestamp } from '@/features/account/cashFlowTime'

test('分段输入补齐两位并始终按北京时间提交，保留秒数', () => {
  expect(cashFlowTimestamp('2026-11-13', '9:7:3')).toBe('2026-11-13T09:07:03+08:00')
  const timestamp = cashFlowTimestamp('2026-11-13', '00:00:47')!
  expect(new Date(timestamp).toISOString()).toBe('2026-11-12T16:00:47.000Z')
  expect(cashFlowTimestamp('2024-02-29', '23:59:59')).toBe('2024-02-29T23:59:59+08:00')
})

test('空值、非法自然日和越界时分秒阻止提交，不替用户猜测发生时间', () => {
  for (const [date, time] of [
    ['', '09:30:00'], ['2026-02-29', '09:30:00'], ['2026-02-30', '09:30:00'],
    ['0000-01-01', '09:30:00'], ['2026-11-13T09:30:00', '10:00:00'],
    ['2026-11-13', ''], ['2026-11-13', '09:30:'], ['2026-11-13', '24:00:00'],
    ['2026-11-13', '09:60:00'], ['2026-11-13', '09:30:60'], ['2026-11-13', '-1:30:00'],
  ]) expect(cashFlowTimestamp(date, time)).toBeNull()
})

test('日期及三个时间分段都必填，编辑值完整回显', () => {
  const html = renderToStaticMarkup(createElement(CashFlowDateTime, {
    date: '2026-11-13', time: '09:30:47', onDateChange: () => {}, onTimeChange: () => {},
  }))
  expect(html).toContain('value="2026-11-13"')
  expect(html).toContain('value="47"')
  expect(html.match(/required=""/g)).toHaveLength(4)
  expect(html).toContain('aria-label="发生时间：秒（北京时间）"')
})

test('未来日期立即提示，完整时间按北京时间精确比较到秒', () => {
  const now = Date.parse('2026-11-13T01:30:00Z')
  for (const [date, time] of [['2026-11-13', '09:30:01'], ['2026-11-14', ''], ['2026-11-14', '00:00:00']])
    expect(cashFlowTimeError(date, time, now)).toBe('发生时间不能晚于当前时间')
  for (const [date, time] of [['2026-11-13', '09:30:00'], ['2026-11-13', '09:29:59'], ['2026-11-12', '23:59:59'], ['', ''], ['2026-11-13', '09:30:']])
    expect(cashFlowTimeError(date, time, now)).toBeNull()
  const midnight = Date.parse('2026-11-12T16:00:00Z')
  expect(cashFlowTimeError('2026-11-13', '00:00:00', midnight)).toBeNull()
  expect(cashFlowTimeError('2026-11-13', '00:00:01', midnight)).toBe('发生时间不能晚于当前时间')
})
