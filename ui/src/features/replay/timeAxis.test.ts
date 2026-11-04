import { expect, it } from 'bun:test'
import { replayChartLayout, replayTimeX } from '@/features/replay/chartGeometry'
import { replayTimeLabelLeft, replayTimeTickOpacity, replayTimeTicks } from '@/features/replay/timeAxis'

const layout = replayChartLayout(900, 300)

it('时间刻度保持真实坐标，采用易读间隔且标签不碰撞', () => {
  const start = 1_793_664_000_000_000
  for (const duration of [1, 10, 99_999, 100_000, 4_999_999, 5_000_000, 60_000_000]) {
    const range = { start_us: start, end_us: start + duration }
    const ticks = replayTimeTicks(range, layout)
    expect(ticks.length).toBeGreaterThan(0)
    expect(new Set(ticks.map(tick => tick.label)).size).toBe(ticks.length)
    for (const [index, tick] of ticks.entries()) {
      expect(tick.time).toBeGreaterThanOrEqual(range.start_us)
      expect(tick.time).toBeLessThan(range.end_us)
      expect(tick.x).toBe(replayTimeX(tick.time, range, layout))
      expect(tick.left).toBeGreaterThanOrEqual(layout.left)
      expect(tick.left + tick.width).toBeLessThanOrEqual(layout.right)
      if (index) expect(tick.left).toBeGreaterThanOrEqual(ticks[index - 1].left + ticks[index - 1].width + 8)
    }
    if (ticks.length > 1) {
      const step = ticks[1].time - ticks[0].time
      expect([1, 2, 5]).toContain(step / 10 ** Math.floor(Math.log10(step)))
    }
  }
})

it('边缘只移动文字，不移动刻线；窄图仍保留可读标签', () => {
  expect(replayTimeLabelLeft(layout.left, 110, layout)).toBe(layout.left)
  expect(replayTimeLabelLeft(layout.right, 110, layout)).toBe(layout.right - 110)
  const narrow = replayChartLayout(240, 90)
  const ticks = replayTimeTicks({ start_us: 1, end_us: 2 }, narrow)
  expect(ticks).toHaveLength(1)
  expect(ticks[0].time).toBe(1)
  expect(ticks[0].left + ticks[0].width).toBeLessThanOrEqual(narrow.right)
})

it('当前读数优先，普通标签按距离让位，反向移动不积累动画', () => {
  expect(replayTimeTickOpacity(100, 80, 140, 110)).toBe(0)
  expect(replayTimeTickOpacity(100, 80, 180, 110)).toBe(0)
  expect(replayTimeTickOpacity(100, 80, 186, 110)).toBe(.5)
  expect(replayTimeTickOpacity(100, 80, 192, 110)).toBe(1)
  expect(replayTimeTickOpacity(186, 110, 100, 80)).toBe(.5)
  expect(replayTimeTickOpacity(100, 80, 186, 110)).toBe(.5)
})
