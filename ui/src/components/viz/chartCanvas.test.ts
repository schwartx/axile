import { expect, test } from 'bun:test'
import { drawSeries, drawValueAxis, keyboardPoint, timeAtX, timeX, zoomTimeViewport, type CanvasTheme } from '@/components/viz/chartCanvas'
import { niceValueAxis } from '@/components/viz/chartAxis'
import { seriesStyle } from '@/components/viz/seriesStyle'
import { createTimeScale } from '@/features/history/timeScale'

const plot = { left: 12, right: 412, top: 16, bottom: 200 }
const view = { start: 0, end: 600 }
const axis = { min: -10, max: 10, step: 10, ticks: [-10, 0, 10] }
function context() {
  const moves: number[][] = [], lines: number[][] = [], bars: number[][] = [], strokes: string[] = []
  const ctx = { strokeStyle: '', fillStyle: '', lineWidth: 1,
    save() {}, restore() {}, beginPath() {}, rect() {}, clip() {}, setLineDash() {}, fillText() {},
    moveTo: (...point: number[]) => moves.push(point), lineTo: (...point: number[]) => lines.push(point),
    fillRect: (...rect: number[]) => bars.push(rect), stroke: () => strokes.push(ctx.strokeStyle),
  }
  return { ctx: ctx as unknown as CanvasRenderingContext2D, moves, lines, bars, strokes }
}

test('缺失估值和新基准都断开，后续可靠观测仍连接，非有限值不送入画布', () => {
  const { ctx, moves, lines } = context()
  drawSeries(ctx, plot, [0, 100, 200, 300, 400, 500, 600], [{ values: [0, 3, null, 0, 2, NaN, 1], baselines: [0, 0, null, 3, 3, 3, 3], color: 'blue' }], view, axis)
  expect(moves.map(point => point[0])).toEqual([12, 212, 412])
  expect(lines.map(point => point[0])).toEqual([timeX(100, plot, view), timeX(400, plot, view)])
  const adjacent = context()
  drawSeries(adjacent.ctx, plot, [0, 100, 200], [{ values: [0, 3, 0], baselines: [0, 0, 2], color: 'blue' }], view, axis)
  expect(adjacent.moves).toHaveLength(2)
  expect(adjacent.lines).toHaveLength(1)
})

test('账户每日双柱继续共用零轴，负收益向下，空值不画柱', () => {
  const { ctx, bars, lines } = context()
  drawSeries(ctx, plot, [0, 300, 600], [{ values: [0, 4, null], color: 'blue' }, { values: [null, -4, 2], color: 'gray' }], view, axis, undefined, true)
  expect(lines).toHaveLength(0)
  expect(bars).toHaveLength(4)
  expect(bars[1][1]).toBeLessThan(bars[2][1])
  expect(bars[1][1] + bars[1][3]).toBeCloseTo(bars[2][1])
  expect(bars[1][0]).toBeLessThan(bars[2][0])
})

test('零轴更清楚，金额和收益轴都覆盖正负值且刻度等距', () => {
  const { ctx, strokes } = context()
  drawValueAxis(ctx, plot, axis, { line: 'grid', muted: 'zero' } as CanvasTheme, String)
  expect(strokes).toEqual(['grid', 'zero', 'grid'])
  for (const values of [[], [0], [-7150, 160000], [-0.07, 0.21]]) {
    const result = niceValueAxis(values, 1)
    expect(result.min).toBeLessThanOrEqual(Math.min(0, ...values))
    expect(result.max).toBeGreaterThanOrEqual(Math.max(0, ...values))
    expect(result.max).toBeGreaterThan(result.min)
    expect(result.ticks.some(value => Math.abs(value) < result.step / 100)).toBe(true)
  }
  expect(niceValueAxis([NaN, Infinity, 0], 1)).toEqual(niceValueAxis([0], 1))
})

test('自然时间和观测序列共享投影、逆投影与缩放，键盘首末点不越界', () => {
  const times = [0, 3600000, 10 * 864e5, 11 * 864e5]
  for (const mode of ['natural', 'observations'] as const) {
    const scale = createTimeScale(mode, times), full = { start: times[0], end: times.at(-1)! }
    for (const time of times) expect(timeAtX(timeX(time, plot, full, scale), plot, full, scale)).toBeCloseTo(time, 3)
    const zoomed = zoomTimeViewport(full, 0.7, times[2], times, scale)
    expect(zoomed.start).toBeGreaterThanOrEqual(full.start)
    expect(zoomed.end).toBeLessThanOrEqual(full.end)
    expect(zoomTimeViewport(zoomed, 100, times[2], times, scale)).toEqual(full)
  }
  expect(keyboardPoint('ArrowLeft', 0, 4)).toBe(0)
  expect(keyboardPoint('ArrowRight', 3, 4)).toBe(3)
  expect(keyboardPoint('Home', 3, 4)).toBe(0)
  expect(keyboardPoint('End', 0, 4)).toBe(3)
  expect(keyboardPoint('Enter', 0, 4)).toBeNull()
  expect(keyboardPoint('End', 0, 0)).toBeNull()
})

test('任意品种的颜色和线型按身份固定，排名、勾选集合和收益翻向不重分配', () => {
  const symbols = ['CNY:DCE.c', 'CNY:SHFE.fu', 'USD:OTHER.new']
  const before = new Map(symbols.map(key => [key, seriesStyle(key)]))
  for (const key of symbols.toReversed().slice(0, 2)) expect(seriesStyle(key)).toEqual(before.get(key)!)
  expect(new Set(symbols.map(key => JSON.stringify(seriesStyle(key)))).size).toBeGreaterThan(1)
  const colors = new Set(Array.from({ length: 50 }, (_, i) => seriesStyle(`product-${i}`).color))
  expect(colors).toEqual(new Set([1, 2, 3, 4, 5].map(i => `var(--color-chart-${i})`)))
})
