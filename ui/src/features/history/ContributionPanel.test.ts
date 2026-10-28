import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { AnalysisChart, ContributionObservation } from '@/lib/api/performance'
import { ContributionPanel } from '@/features/history/ContributionPanel'

function render(last: Partial<ContributionObservation> = {}) {
  const row = { symbol: 'c2701', product: 'DCE.c', currency: 'CNY', fee: 0, fee_missing: 0, issues: 0, trades: 0, gross_pnl: 0 }
  const chart = {
    result: { points: [1000, 1100].map((equity, idx) => ({ date: `2026-10-0${idx + 1}`, observed_at: `2026-10-0${idx + 1}T15:00:00`, account_equity: equity })) },
    contributions: [{ ...row, idx: 0 }, { ...row, idx: 1, ...last }],
  } as AnalysisChart
  return renderToStaticMarkup(createElement(ContributionPanel, { chart, selection: null, scaleMode: 'natural' }))
}

test('不完整的毛盈亏汇总明确标识，保留待核对与费用缺失原因', () => {
  const html = render({ issues: 1, fee_missing: 3 })
  expect(html).toContain('部分品种未计入毛盈亏汇总')
  expect(html).toContain('手续费未完整')
  expect(html).toContain('待核对')
  expect(html).toContain('费用缺失')
  expect(html).toContain('3 笔费用未知')
  expect(html).toContain('aria-expanded="false"')
  expect(html).toContain('aria-hidden="true"')
  expect(html).toContain('选择品种查看曲线')
})

test('真实零盈亏不显示缺失警告，费用缺失仍保留可用毛盈亏曲线', () => {
  const complete = render()
  expect(complete).not.toContain('部分品种未计入')
  expect(complete).not.toContain('手续费未完整')
  expect(complete).toContain('累计毛盈亏贡献')
  const missingFee = render({ gross_pnl: 100, fee_missing: 1 })
  expect(missingFee).not.toContain('部分品种未计入')
  expect(missingFee).toContain('费用缺失')
  expect(missingFee).not.toContain('选择品种查看曲线')
})
