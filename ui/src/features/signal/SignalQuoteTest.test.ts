import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { QuoteDetails } from './SignalQuoteTest'
import type { SignalQuoteTestResult } from './signalApi'

type Depth = NonNullable<SignalQuoteTestResult['items'][number]['quote']>['bids']

function renderDepth(bids: Depth, asks: Depth) {
  return renderToStaticMarkup(createElement(QuoteDetails, {
    symbol: '600000.SH',
    quote: { last_price: 10, timestamp: Date.UTC(2026, 10, 25), bids, asks },
    error: null,
    currency: 'CNY',
  }))
}

test('盘口围绕最新成交价排列，双边挂量共用单档比例', () => {
  const asks: Depth = [[10.01, 100], [10.02, 300]]
  const html = renderDepth([[9.99, 100], [9.98, 400]], asks)

  expect([...html.matchAll(/>(10\.02|10\.01|￥10|9\.99|9\.98)</g)].map((match) => match[1]))
    .toEqual(['10.02', '10.01', '￥10', '9.99', '9.98'])
  expect([...html.matchAll(/style="width:([^"]+)"[^>]*><\/span><span class="relative">(\d+)<\/span>/g)]
    .map((match) => [match[1], match[2]]))
    .toEqual([['75%', '300'], ['25%', '100'], ['25%', '100'], ['100%', '400']])
  expect(html.slice(html.indexOf('最新成交')).split('</tr>', 1)[0]).toContain('北京时间 2026/11/25 08:00:00')
  expect(asks[0][0]).toBe(10.01)
})

test('单边盘口保留原始小数挂量，缺失的一边有明确提示', () => {
  const html = renderDepth([[9.99, 0.25], [9.98, 0]], [])

  expect(html).toContain('未提供卖盘')
  expect(html).toContain('style="width:100%"')
  expect(html).toContain('style="width:0%"')
  expect(html).toContain('>0.25</span>')
  expect(renderDepth([], [])).toContain('此次报价未包含盘口档位。')
})

test('盘口每侧最多显示最优五档，挂量比例按可见档位计算', () => {
  const bids: Depth = [[9.99, 100], [9.98, 200], [9.97, 300], [9.96, 400], [9.95, 500], [9.94, 10000]]
  const asks: Depth = [[10.01, 100], [10.02, 200], [10.03, 300], [10.04, 400], [10.05, 500], [10.06, 10000]]
  const html = renderDepth(bids, asks)

  expect([...html.matchAll(/>((?:9|10)\.\d{2})<\/td>/g)].map((match) => match[1]))
    .toEqual(['10.05', '10.04', '10.03', '10.02', '10.01', '9.99', '9.98', '9.97', '9.96', '9.95'])
  expect(html.match(/style="width:100%"/g)).toHaveLength(2)
})
