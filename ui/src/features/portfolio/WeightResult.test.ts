import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { WeightResult } from '@/features/portfolio/WeightResult'

test('数量结果保留小数，省略权重敞口摘要', () => {
  const html = renderToStaticMarkup(createElement(WeightResult, { mode: 'quantity', target: { XAUUSD: 0.015, SMALL: 0.00000015 } }))
  expect(html).toContain('返回数量')
  expect(html).toContain('0.015')
  expect(html).toContain('0.00000015')
  expect(html).not.toContain('总敞口')
  expect(html).not.toContain('净敞口')
})
