import { expect, test } from 'bun:test'
import type { AnalysisChart, ContributionObservation } from '@/lib/api/performance'
import { contributionView } from '@/features/history/contributions'
import { pointTime } from '@/features/history/chartModel'

function chart(rows: Partial<ContributionObservation>[] = []): AnalysisChart {
  return {
    result: { points: [1000, 1200, 900, 1100].map((equity, idx) => ({ date: `2026-10-0${idx + 1}`, observed_at: `2026-10-0${idx + 1}T15:00:00`, account_equity: equity })) },
    contributions: rows.map(row => ({ symbol: 'fu2701', product: 'SHFE.fu', currency: 'CNY', fee: 0, fee_missing: 0, issues: 0, trades: 0, gross_pnl: 0, idx: 0, ...row })),
  } as AnalysisChart
}

test('roll months aggregate and closed contracts remain in rankings', () => {
  const source = chart([
    { idx: 0 }, { idx: 3, gross_pnl: 120, trades: 2 },
    { idx: 1, symbol: 'fu2705' }, { idx: 3, symbol: 'fu2705', gross_pnl: -20 },
    { idx: 0, symbol: 'c2701', product: 'DCE.c' }, { idx: 3, symbol: 'c2701', product: 'DCE.c', gross_pnl: -40 },
  ])
  const result = contributionView(source, null)!
  expect(result.groups.map(g => [g.product, g.gross])).toEqual([['SHFE.fu', 100], ['DCE.c', -40]])
  expect(result.groups[0].contracts).toHaveLength(2)
  expect(result.unexplained).toBe(40)
})

test('selection rebases each series and changes the ranking for that interval', () => {
  const source = chart([{ idx: 0 }, { idx: 1, gross_pnl: 200 }, { idx: 2, gross_pnl: 50 }, { idx: 3, gross_pnl: 100 }])
  const points = source.result.points
  const result = contributionView(source, { kind: 'interval', start: pointTime(points[1]), end: pointTime(points[3]) })!
  expect(result.groups[0].values).toEqual([0, -150, -100])
  expect(result.groups[0].gross).toBe(-100)
  expect(result.accountChange).toBe(-100)
})

test('missing fees keep gross but unknown net; missing fills break the curve', () => {
  const source = chart([{ idx: 0 }, { idx: 1, gross_pnl: 100 }, { idx: 2, gross_pnl: 80, issues: 1 }, { idx: 3, gross_pnl: 90, issues: 1, fee_missing: 1 }])
  const result = contributionView(source, null)!.groups[0]
  expect(result.gross).toBeNull()
  expect(result.values).toEqual([0, 100, null, null])
  expect(result.net).toBeNull()
  const clean = chart([{ idx: 0 }, { idx: 3, gross_pnl: 100, fee_missing: 1 }])
  expect(contributionView(clean, null)!.groups[0].gross).toBe(100)
  expect(contributionView(clean, null)!.groups[0].net).toBeNull()
})

test('a later interval can be valid after an earlier missing fill', () => {
  const source = chart([{ idx: 1, gross_pnl: 100, issues: 1 }, { idx: 2, gross_pnl: 80, issues: 1 }, { idx: 3, gross_pnl: 90, issues: 1 }])
  const points = source.result.points
  expect(contributionView(source, { kind: 'interval', start: pointTime(points[1]), end: pointTime(points[3]) })!.groups[0].gross).toBe(-10)
})

test('different currencies never combine into an account amount', () => {
  const source = chart([{ idx: 0 }, { idx: 3, gross_pnl: 100 }, { idx: 0, symbol: 'OTHER', currency: 'USD' }, { idx: 3, symbol: 'OTHER', currency: 'USD', gross_pnl: 50 }])
  expect(contributionView(source, null)!.knownGross).toBeNull()
  expect(contributionView(source, null)!.unexplained).toBeNull()
})
