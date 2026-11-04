import { replayTimeX, type ReplayChartLayout } from '@/features/replay/chartGeometry'
import { replayTime } from '@/features/replay/presentation'
import type { ReplayRange } from '@/features/replay/types'

/** 读数在边缘让位，刻线仍保留真实时间坐标。 */
export function replayTimeLabelLeft(x: number, width: number, layout: ReplayChartLayout): number {
  return Math.max(layout.left, Math.min(layout.right - width, x - width / 2))
}

/** 时间间隔采用 1 / 2 / 5，且不小于文字能表达的精度。 */
export function replayTimeTicks(range: ReplayRange, layout: ReplayChartLayout, characterWidth = 6.6) {
  const duration = range.end_us - range.start_us
  const precision = duration < 100_000 ? 1 : duration < 5_000_000 ? 1000 : 1_000_000
  const format = (time: number) => precision === 1 ? replayTime(time) : precision === 1000 ? replayTime(time).slice(0, -3) : replayTime(time, false, false)
  const width = format(range.start_us).length * characterWidth + 8
  const desired = Math.max(precision, duration * (width + 24) / Math.max(1, layout.right - layout.left))
  const magnitude = 10 ** Math.floor(Math.log10(desired))
  const step = [1, 2, 5, 10].find(value => value * magnitude >= desired)! * magnitude
  const first = Math.ceil(range.start_us / step)
  const count = Math.max(0, Math.ceil(range.end_us / step) - first)
  const times = count ? Array.from({ length: count }, (_, index) => (first + index) * step) : [range.start_us + Math.floor((duration - 1) / 2)]
  let previousRight = -Infinity
  return times.flatMap(time => {
    const x = replayTimeX(time, range, layout), label = format(time)
    const left = replayTimeLabelLeft(x, width, layout)
    if (left < previousRight + 8) return []
    previousRight = left + width
    return [{ time, x, label, width, left }]
  })
}

/** 当前时间优先占位；普通标签按距离退墨，不追赶播放头。 */
export function replayTimeTickOpacity(left: number, width: number, currentLeft: number, currentWidth: number): number {
  const gap = Math.max(currentLeft - left - width, left - currentLeft - currentWidth)
  return Math.max(0, Math.min(1, gap / 12))
}
