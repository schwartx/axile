import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { WeightResult } from '@/features/portfolio/WeightResult'
import { SnapshotResult } from '@/features/portfolio/SnapshotResult'

test('数量结果保留小数，省略权重敞口摘要', () => {
  const html = renderToStaticMarkup(createElement(WeightResult, { mode: 'quantity', target: { XAUUSD: 0.015, SMALL: 0.00000015 } }))
  expect(html).toContain('目标持仓')
  expect(html).toContain('0.015')
  expect(html).toContain('0.00000015')
  expect(html).not.toContain('总敞口')
  expect(html).not.toContain('净敞口')
})

test('期货试跑用多空和手表达目标持仓', () => {
  const html = renderToStaticMarkup(createElement(WeightResult, {
    mode: 'quantity', quantityUnit: '手', target: { FG701: 2, MA701: -1, SM701: -1 },
  }))
  expect(html).toContain('多 2 手')
  expect(html).toContain('空 1 手')
  expect(html).toContain('个目标品种')
  expect(html).not.toContain('品种数量')
  expect(html).not.toContain('返回数量')
})

test('生效目标与试跑一致，按渠道单位显示多空持仓', () => {
  const html = renderToStaticMarkup(createElement(SnapshotResult, {
    quantityUnit: '股', weights: {
      data: { target_mode: 'quantity', raw_targets: { A: 200, B: -100 }, weights: {}, calculated_at: '2026-11-06' },
      loading: false, error: null, recalculateError: null, stale: false,
    } as unknown as Parameters<typeof SnapshotResult>[0]['weights'],
  }))
  expect(html).toContain('目标持仓')
  expect(html).toContain('多 200 股')
  expect(html).toContain('空 100 股')
  expect(html).not.toContain('总敞口')
})

test('权重模式保留百分比口径，空仓仍明确显示', () => {
  const html = renderToStaticMarkup(createElement(WeightResult, { target: { A: 0.2, B: -0.1 }, quantityUnit: '手' }))
  expect(html).toContain('返回权重')
  expect(html).toContain('0.2000')
  expect(html).toContain('-0.1000')
  expect(html).not.toContain('多 0.2 手')
  const flat = renderToStaticMarkup(createElement(WeightResult, { mode: 'quantity', target: { A: 0 } }))
  expect(flat).toContain('返回空仓')
})
