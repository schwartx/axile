import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { HoldingsView } from './HoldingsView'
import type { AccountRebalancePlan, TargetSizing } from '@/types/api'

const plan: AccountRebalancePlan = {
  rows: [{ symbol: 'SM701', current_weight: 0, target_weight: 0.02,
    current_quantity: 0, target_quantity: 0, aligned: true, action: 'aligned', side: 'none' }],
  off_symbol_count: 0, observed_at: '2026-11-17T10:00:00', target_calculated_at: '2026-11-17T09:59:00',
}
const sizing: TargetSizing = {
  status: 'available', execution_id: 'exec-sized', calculated_at: plan.target_calculated_at,
  rows: { SM701: {
    symbol: 'SM701', sizing_mode: 'weight', status: 'SIZED', reason_code: 'COMMON.SIZING.BELOW_MIN_QUANTITY',
    strategy_weight: 0.02, account_weight: 0.02, account_multiplier: 1, weight_precision: 0.01,
    equity: 1_000_000, reference_price: null, unit_multiplier: null, unit_notional: null,
    target_notional: 20_000, raw_quantity: 0.2, target_quantity: 0, current_quantity: 0,
    quantity_step: 1, min_quantity: 1, min_notional: null,
  } },
}

function render(executablePlan: AccountRebalancePlan | null) {
  return renderToStaticMarkup(createElement(HoldingsView, {
    executablePlan, sizing, positions: [], target: { SM701: 0.02 }, equity: 1_000_000,
    assetLabel: '权益', quantityLabel: '手',
  }))
}

test('零目标空仓显示无需下单，汇总计为量化为0', () => {
  const html = render(plan)
  expect(html).toContain('无需下单')
  expect(html).toContain('1 量化为0')
  expect(html).not.toContain('待调整')
})

test('未就绪的对账不显示空仓或无需下单', () => {
  for (const result of [null, { ...plan, rows: [], off_symbol_count: null }]) {
    const html = render(result)
    expect(html).toContain('尚未就绪')
    expect(html).not.toContain('当前空仓')
    expect(html).not.toContain('无需下单')
  }
})

test('真实空目标和空仓仍显示空仓', () => {
  expect(render({ ...plan, rows: [] })).toContain('当前空仓，且无目标持仓')
})
