import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { SignalQuotesDocPage } from './SignalQuotesDocPage'

test('renders the quote contract and a copyable example', () => {
  const html = renderToStaticMarkup(createElement(SignalQuotesDocPage))

  expect(html).toContain('自定义行情函数')
  expect(html).toContain('href="#example"')
  expect(html).toContain('id="example"')
  expect(html).toContain('aria-label="复制代码"')
  expect(html).toContain('复制 Markdown')
  expect(html).toContain('def fetch_quotes(symbols: list[str])')
  expect(html).toContain('买一和卖一放在各自列表首位')
})
