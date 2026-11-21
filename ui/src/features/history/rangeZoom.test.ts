import { expect, test } from 'bun:test'
import { timeX } from '@/components/viz/chartCanvas'
import { rangeContributionRows, rangeReturnPoints, rangeZoom } from './rangeZoom'

test.each(['natural', 'observations'] as const)('范围缩放固定最右观测 - %s', mode => {
  const plot = { left: 10, right: 110, top: 0, bottom: 20 }
  const zoom = rangeZoom({ start: 40, end: 90 }, { start: 10, end: 90 }, [10, 20, 40, 90], mode)!
  expect(zoom.viewport(0)).toEqual({ start: 40, end: 90 })
  expect(zoom.viewport(1)).toEqual({ start: 10, end: 90 })
  expect(timeX(90, plot, zoom.viewport(0.5), zoom.scale)).toBe(plot.right)
  expect(timeX(40, plot, zoom.viewport(0.5), zoom.scale)).toBeGreaterThan(plot.left)
  expect(rangeZoom({ start: 10, end: 90 }, { start: 10, end: 90 }, [10, 90], mode)).toBeNull()
})

test('扩展较早数据时保留当前窗口的收益数值和已有断点', () => {
  const point = (date: string, value: number) => ({
    date, account_return: value, portfolio_return: null, account_daily_return: null,
    portfolio_daily_return: null, difference: null,
  })
  const middle = point('2026-01-02', 0), latest = point('2026-01-03', .22)
  const expanded = rangeReturnPoints([middle, latest], [point('2026-01-01', 0), point('2026-01-02', .1), point('2026-01-03', .34)])
  expect(expanded[0].account_return).toBeCloseTo(1 / 1.1 - 1)
  expect(expanded[1]).toBe(middle)
  expect(expanded[2]).toBe(latest)

  const old = { symbol: 'actual', product: '实盘', values: [0, 3], baselines: [0, 0] }
  const next = { ...old, values: [8, 0, 2, 5], baselines: [0, 1, 1, 1] }
  expect(rangeContributionRows([old], [next], [30, 40], [10, 20, 30, 40])).toEqual({
    times: [10, 20, 30, 40],
    rows: [{ ...old, values: [8, -2, 0, 3], baselines: [0, 1, 1, 1] }],
  })
})
