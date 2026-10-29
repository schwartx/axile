import { describe, expect, it } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { buildExecutionDetail } from '@/features/account/executionDetail'
import { ExecutionEvidence } from '@/features/history/ExecutionEvidence'

function renderEvidence(includeUnreached = false, noop = false) {
  const symbols = [
    { symbol: 'ag2612', status: noop ? 'NOOP' : 'SUCCEEDED', before: 0, target: noop ? 0 : 2, after: noop ? 0 : 2, filled: noop ? 0 : 2, avg_price: noop ? null : 1782.73, reached: true },
    ...(includeUnreached ? [{ symbol: 'rb2610', status: 'PARTIAL', before: 0, target: 3, after: 1, filled: 1, avg_price: 3000, reached: false }] : []),
  ]
  const summary = {
      status: noop ? 'NOOP' : 'PARTIAL', error: noop ? null : '最终账户查询失败',
      symbol_results: Object.fromEntries(symbols.map(s => [s.symbol, { status: s.status, final_volume: s.after }])),
      reconciliation: { account: { source_before: 'real', source_after: null }, symbols },
  }
  const model = buildExecutionDetail([], [], null, summary)
  return renderToStaticMarkup(createElement(ExecutionEvidence, { model, currency: 'CNY' }))
}

describe('ExecutionEvidence', () => {
  it('整体收尾失败仍展示已到位品种的动作和成交均价', () => {
    const html = renderEvidence()
    expect(html).toContain('ag2612')
    expect(html).toContain('建仓')
    expect(html).toContain('1,782.73')
    expect(html).toContain('已到位')
    expect(html).not.toContain('本次无调仓动作')
  })

  it('部分到位同时保留已完成和未完成品种', () => {
    const html = renderEvidence(true)
    expect(html).toContain('ag2612')
    expect(html).toContain('rb2610')
    expect(html).toContain('已到位')
    expect(html).toContain('未到位')
  })

  it('持仓不变且没有成交仍展示无调仓动作', () => {
    const html = renderEvidence(false, true)
    expect(html).toContain('本次无调仓动作')
    expect(html).not.toContain('ag2612')
  })
})
