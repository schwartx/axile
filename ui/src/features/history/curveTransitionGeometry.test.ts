import { expect, test } from 'bun:test'
import { curvePath, sparklineCoordinates } from '@/components/viz/sparklineGeometry'
import { chartAxis, PLOT, returnY, xPosition, type ChartScene } from '@/components/viz/performanceCanvas'
import { curveDurationMs, fitCurve, interpolateCurve, performanceCoordinates, translateCurve } from '@/features/history/curveTransitionGeometry'
import { pointTime } from '@/features/history/chartModel'
import type { AccountPerformance, PerformancePoint } from '@/types/api'

const point = (day: number, value: number | null, portfolio = value): PerformancePoint => ({
  date: `2026-01-${String(day).padStart(2, '0')}`, observed_at: `2026-01-${String(day).padStart(2, '0')}T00:00:00Z`,
  account_return: value, portfolio_return: portfolio, account_daily_return: null, portfolio_daily_return: null, difference: null,
})
const points = [point(1, -.1), point(2, .2, .7), point(3, null), point(7, .05), point(9, .4)]
const times = points.map(pointTime)
const scene: ChartScene = {
  data: { points } as AccountPerformance, times, width: 900,
  viewport: { start: times[0] - 864e5, end: times.at(-1)! + 864e5 }, daily: false,
  portfolioNames: new Map(),
  theme: { bg: '', surface: '', ink: '', muted: '', line: '', accent: '', warn: '', fill: '', font: '' },
}

test('生产 CSS 压缩前后时长一致，缺失或无效 token 使用默认时长', () => {
  expect(curveDurationMs('360ms', 100)).toBe(360)
  expect(curveDurationMs(' .36s ', 100)).toBe(360)
  expect(curveDurationMs('.28s', 100)).toBe(280)
  expect(curveDurationMs('1600ms', 100)).toBe(1600)
  for (const value of ['', 'oops', '-1s', '0ms', '360']) expect(curveDurationMs(value, 360)).toBe(360)
})

test('展开首帧与小图一致，末帧使用包含回测/执行时间范围的真实 Canvas 轴', () => {
  const source = translateCurve(sparklineCoordinates(points, 150, 46), 123, 245)
  const target = translateCurve(performanceCoordinates(scene), 40, 110)
  expect(interpolateCurve(source, target, 0)).toEqual(source)
  const last = interpolateCurve(source, target, 1)
  const axis = chartAxis(scene)
  expect(last[1]!.x).toBeCloseTo(xPosition(times[1], 900, scene.viewport) + 40)
  expect(last[1]!.y).toBeCloseTo(returnY(.2, axis) + 110)
  expect(last[0]!.x).toBeGreaterThan(PLOT.left + 40)
  expect(last[1]!.y).toBeGreaterThan(PLOT.top + 110)
  expect(last[2]).toBeNull()
  expect(curvePath(last).match(/M/g)).toHaveLength(2)
})

test('空值/无效时间不被补齐，横线和不足两条观测有稳定几何', () => {
  expect(sparklineCoordinates([point(1, 0)], 150, 46)).toEqual([])
  expect(sparklineCoordinates([point(1, 0), point(3, 0)], 150, 46)).toEqual([{ x: 3, y: 23 }, { x: 147, y: 23 }])
  const invalid = { ...point(1, 0), observed_at: 'invalid' }
  const result = sparklineCoordinates([invalid, ...points], 150, 46)
  expect(result[0]).toBeNull()
  expect(curvePath(result)).not.toContain('NaN')
  expect(performanceCoordinates(scene, [invalid, point(2, Infinity)])).toEqual([null, null])
})

test('版本不同只对原曲线做空间变换，不借目标点数生成观测或跨断点', () => {
  const source = [{ x: 0, y: 0 }, null, { x: 10, y: 20 }, { x: 20, y: 10 }]
  const target = [{ x: 100, y: 200 }, { x: 500, y: 600 }]
  expect(fitCurve(source, target)).toEqual([{ x: 100, y: 200 }, null, { x: 300, y: 600 }, { x: 500, y: 400 }])
  expect(fitCurve([{ x: 0, y: 4 }, { x: 2, y: 4 }], target)).toEqual([{ x: 100, y: 400 }, { x: 500, y: 400 }])
  expect(fitCurve(source, [])).toEqual([])
})
