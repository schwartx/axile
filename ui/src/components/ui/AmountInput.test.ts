import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { AmountInput } from './AmountInput'

test('通用金额输入保留原文、币种单位和量级，并关联外部校验提示', () => {
  const html = renderToStaticMarkup(createElement(AmountInput, {
    value: '123123', unit: '元', ariaLabel: '账户权益', onChange() {}, invalid: true, describedBy: 'form-error',
  }))
  expect(html).toContain('value="123123"')
  expect(html).toContain('12.3123万元')
  expect(html).toContain('aria-invalid="true"')
  expect(html).toMatch(/aria-describedby="[^"]+ form-error"/)
  expect(html).toContain('aria-label="账户权益"')
})

test('行内模式把量级放在同一个输入框边框内', () => {
  const html = renderToStaticMarkup(createElement(AmountInput, {
    value: '123', unit: '元', ariaLabel: '金额', onChange() {}, inlineHint: true,
  }))
  expect(html).toMatch(/<input[^>]*value="123"[^>]*><span[^>]*role="status"[^>]*>1\.23百元<\/span><\/span>/)
})
