import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { HoldingsView } from '@/features/account/HoldingsView'
import { rebalanceHeadline } from '@/features/account/holdingPreview'
import { portfolioTargetSummary } from '@/features/portfolio/portfolioCardSummary'
import { rebalancePlanOfServer } from '@/lib/derive'
import type { AccountRebalancePlan, TargetSizingRow, TargetWeightSnapshot } from '@/types/api'

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
  expect(html).toContain('数量目标 → 可执行目标')
  expect(html).toContain('数量标尺逐品种缩放')
  expect(html).toContain('多34,700股')
  expect(html).toContain('多1,900股')
  expect(html).toContain('0股')
  expect(html).toContain('aria-label="查看 513050.SH 持仓与目标"')
  expect(html).not.toContain('当前空仓，且无目标持仓')
  expect(html).not.toContain('净敞口')
  expect(html.match(/left:50%;width:44%/g)).toHaveLength(2)
})

test('数量对照保留小数、空头和未知持仓，不把未知数量当零', () => {
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: { ...comparison, off_symbol_count: 1, rows: [
      { ...comparison.rows[0], symbol: 'XAUUSD', current_quantity: null, target_quantity: -0.015, side: 'sell' },
    ] }, target: {}, equity: 0, assetLabel: '市值',
  }))
  expect(html).toContain('空0.015')
  expect(html).toContain('—')
  expect(html).toContain('待确认')
  expect(html).toContain('持仓待确认')
  expect(html).not.toContain('0股')
  expect(html).not.toContain('chart-hatch absolute')
  expect(html).not.toContain('style="left:')
})

test('数量摘要与状态取服务端数量，已到位保持中性并保留资产', () => {
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: { ...comparison, off_symbol_count: 1, rows: [
      { ...comparison.rows[0], current_quantity: 8600, target_quantity: 7500, action: 'reduce', side: 'sell' },
      { ...comparison.rows[1], current_quantity: 1800, target_quantity: 1800, action: 'aligned', aligned: true, side: 'none' },
    ] }, target: {}, rawTargets: { '513050.SH': 7500 }, equity: 100000, assetLabel: '资产', quantityLabel: '股',
  }))
  expect(html).toContain('2 只')
  expect(html).toContain('1 到位')
  expect(html).toContain('当前 持有 2 只')
  expect(html).toContain('资产 10.0万')
  expect(html).toContain('实际 多8,600股')
  expect(html).toContain('待调整')
  expect(html).toContain('text-ink-3"> · 到位')
  expect(html).not.toContain('净敞口')
})

test('数量翻向保留两侧标尺，小数目标沿用数量解释与取整依据', () => {
  const evidence: TargetSizingRow = {
    symbol: 'XAUUSD', sizing_mode: 'quantity', status: 'SIZED', reason_code: 'COMMON.SIZING.QUANTIZED',
    strategy_weight: null, account_weight: null, account_multiplier: null, weight_precision: null,
    equity: null, reference_price: null, unit_multiplier: null, unit_notional: null, target_notional: null,
    raw_quantity: -0.0159, target_quantity: -0.015, current_quantity: 0.01,
    quantity_step: 0.001, min_quantity: 0.001, min_notional: null,
  }
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: { ...comparison, rows: [
      { ...comparison.rows[0], symbol: 'XAUUSD', current_quantity: 0.01, target_quantity: -0.015, action: 'flip', side: 'sell' },
    ] }, target: {}, equity: 0, assetLabel: '资产',
    sizing: { status: 'available', calculated_at: null, execution_id: null, rows: { XAUUSD: evidence } },
  }))
  expect(html).toContain('text-up">多0.01')
  expect(html).toContain('text-down">空0.015')
  expect(html).toContain('bg-warn-mid')
  expect(html).toContain('left:6%;width:44%')
  expect(html).toContain('多0.01')
  expect(html).toContain('空0.0159 → 空0.015 · 按0.001单位取整')
  expect(html).toContain('aria-expanded="false"')
  expect(html).toContain('直接数量目标 空0.0159')
  expect(html).not.toContain('权益')
})

test('数量目标未知时不画调整段、不宣称到位；空列表沿用共同空态', () => {
  const props = { positions: [], executablePlan: { ...comparison, rows: [
    { ...comparison.rows[0], current_quantity: 100, target_quantity: null },
  ] }, target: {}, equity: 0, assetLabel: '资产', quantityLabel: '股' }
  const html = renderToStaticMarkup(createElement(HoldingsView, props))
  expect(html).toContain('实际 多100股')
  expect(html).toContain('待确认')
  expect(html).not.toContain('chart-hatch absolute')
  expect(renderToStaticMarkup(createElement(HoldingsView, {
    ...props, executablePlan: { ...comparison, rows: [] },
  }))).toContain('当前空仓，且无目标持仓')
})

test('权重模式的买卖统计与百分比行保留', () => {
  const weightPlan: AccountRebalancePlan = { ...comparison, target_mode: 'weight', off_symbol_count: 1, rows: [
    { ...comparison.rows[0], current_weight: 0.1, target_weight: 0.2 },
  ] }
  expect(rebalancePlanOfServer(weightPlan)).toMatchObject({ off: 1, buys: 1, rows: [{ cur: 10, tgt: 20, amount: 10 }] })
  expect(rebalanceHeadline(weightPlan, 1)).toBe('1 只待调整')
  const html = renderToStaticMarkup(createElement(HoldingsView, {
    positions: [], executablePlan: weightPlan, target: { '513050.SH': 0.2 }, equity: 100, assetLabel: '权益',
  }))
  expect(html).toContain('账户目标 → 可执行目标')
  expect(html).toContain('多10.0%')
  expect(html).toContain('多20.0%')
  expect(html).toContain('净敞口')
  expect(html).not.toContain('逐品种缩放')
})
