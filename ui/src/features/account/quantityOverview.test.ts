import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { HoldingsView } from '@/features/account/HoldingsView'
import { rebalanceHeadline } from '@/features/account/holdingPreview'
import { portfolioTargetSummary } from '@/features/portfolio/portfolioCardSummary'
import { rebalancePlanOfServer } from '@/lib/derive'
import type { AccountRebalancePlan, TargetWeightSnapshot } from '@/types/api'

const targets = { '513050.SH': 34700, '513090.SH': 1900 }
const comparison: AccountRebalancePlan = {
  target_mode: 'quantity', off_symbol_count: 2,
  observed_at: '2026-11-09T01:17:00Z', target_calculated_at: '2026-11-09T01:17:00Z',
  rows: Object.entries(targets).map(([symbol, target_quantity]) => ({
    symbol, current_weight: null, target_weight: null, current_quantity: 0, target_quantity,
    action: 'open', side: 'buy', aligned: false,
  })),
}
const snapshot: TargetWeightSnapshot = {
  target_mode: 'quantity', raw_targets: targets, weights: {}, account_weights: null,
  calculated_at: comparison.target_calculated_at, source: 'execution', execution_id: 'quantity', context_account_id: 1,
}

test('数量目标空仓保留待建仓结论与买卖统计，不依赖百分比行', () => {
  expect(portfolioTargetSummary(snapshot).activeCount).toBe(2)
  expect(rebalanceHeadline(comparison, 0)).toBe('空仓 · 2 只待建仓')
  const plan = rebalancePlanOfServer(comparison)
  expect(plan).toMatchObject({ off: 2, buys: 2, sells: 0, flips: 0 })
  expect(plan.rows).toEqual([])
  const sell: AccountRebalancePlan = { ...comparison, off_symbol_count: 1, rows: [
    { ...comparison.rows[0], current_quantity: 100, target_quantity: -100, action: 'flip', side: 'sell' },
  ] }
  expect(rebalancePlanOfServer(sell)).toMatchObject({ off: 1, buys: 0, sells: 1, flips: 1 })
  expect(rebalanceHeadline(sell, 1)).toBe('1 只待调整')
})

test('已到位、真实空仓和未计算保持各自结论，空图表不能覆盖偏离', () => {
  expect(rebalanceHeadline({ ...comparison, off_symbol_count: 0, rows: comparison.rows.map(row => ({
    ...row, current_quantity: row.target_quantity, action: 'aligned', side: 'none', aligned: true,
  })) }, 2)).toBe('已调仓到位')
  expect(rebalanceHeadline({ ...comparison, off_symbol_count: 0, rows: [] }, 0)).toBe('空仓 · 与目标一致')
  expect(rebalanceHeadline({ ...comparison, rows: [] }, 0)).toBe('2 只待调整')
  expect(rebalanceHeadline(null, null)).toBe('偏离尚未计算')
  expect(rebalanceHeadline({ ...comparison, off_symbol_count: null }, 0)).toBe('偏离尚未计算')
})

test('数量对照直接显示当前和目标股数，并保留品种详情入口', () => {
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: comparison, target: {}, equity: 0, assetLabel: '市值',
    quantityLabel: '股', onSelect: () => {},
  }))
  expect(html).toContain('当前数量')
  expect(html).toContain('目标数量')
  expect(html).toContain('多34,700股')
  expect(html).toContain('多1,900股')
  expect(html).toContain('0股')
  expect(html).toContain('aria-label="查看 513050.SH 持仓与目标"')
  expect(html).not.toContain('当前空仓，且无目标持仓')
  expect(html).not.toContain('%')
})

test('数量对照保留小数、空头和未知持仓，不把未知数量当零', () => {
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: { ...comparison, off_symbol_count: 1, rows: [
      { ...comparison.rows[0], symbol: 'XAUUSD', current_quantity: null, target_quantity: -0.015, side: 'sell' },
    ] }, target: {}, equity: 0, assetLabel: '市值',
  }))
  expect(html).toContain('空0.015')
  expect(html).toContain('—')
  expect(html).not.toContain('0股')
})

test('权重模式的买卖统计与百分比行保留', () => {
  const weightPlan: AccountRebalancePlan = { ...comparison, target_mode: 'weight', off_symbol_count: 1, rows: [
    { ...comparison.rows[0], current_weight: 0.1, target_weight: 0.2 },
  ] }
  expect(rebalancePlanOfServer(weightPlan)).toMatchObject({ off: 1, buys: 1, rows: [{ cur: 10, tgt: 20, amount: 10 }] })
  expect(rebalanceHeadline(weightPlan, 1)).toBe('1 只待调整')
})
