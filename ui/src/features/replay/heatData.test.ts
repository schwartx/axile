import { expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import init, { Heat } from './heat-wasm/axile_replay_heat'
import { packHeat } from './heatData'
import { replayHeatCells, replayChartLayout } from './chartGeometry'
import type { ReplayQuote } from './types'
await init({ module_or_path: readFileSync(new URL('./heat-wasm/axile_replay_heat_bg.wasm', import.meta.url)) })

it('WASM matches independent JS heat aggregation across bounds, peaks, unknowns, gaps and fractional prices', () => {
  let seed = 31
  const random = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32
  const quotes: ReplayQuote[] = Array.from({ length: 200 }, (_, i) => ({ symbol: 'X', time_us: Math.floor(i / 2) * 100, capture_seq: i, book_valid: i % 17 !== 0, available: true,
    valid_until_us: i % 13 === 0 ? null : Math.floor(i / 2) * 100 + 150,
    bids: Array.from({ length: 10 }, () => ({ price: 1 + Math.floor(random() * 20) * .0001, quantity: random() < .1 ? null : Math.floor(random() * 100) })), asks: [],
  } as unknown as ReplayQuote))
  const gaps = [{ start_us: 4200, end_us: 4700, symbol: 'X' }], end = 9500
  const kernel = new Heat(packHeat([{ quotes, gaps, end }])[0])
  const layout = replayChartLayout(1000, 400)
  for (const start_us of [0, 4100, 4250, 4600, 9300]) for (const tick of [.0001, .00001]) {
    const range = { start_us, end_us: start_us + 300 }, view = { min: 1, max: 1.003, tick }
    const expected = replayHeatCells(quotes, range, end, view, layout, gaps).cells
    const actual = kernel.query(start_us, range.end_us, view.min, view.max, tick, layout.left, layout.right, layout.top, layout.bottom)
    expect([...actual]).toEqual(expected.flatMap(cell => [cell.x, cell.y, cell.quantity ?? NaN, Number(cell.unknown), cell.minPrice, cell.maxPrice]))
  }
  kernel.free()
})

it('WASM rejects invalid intervals and invalid query boundaries', () => {
  expect(() => new Heat(new Float64Array([1, 0, 2, 1]))).toThrow()
  const kernel = new Heat(new Float64Array())
  expect(() => kernel.query(1, 0, 1, 2, 1, 0, 1, 0, 1)).toThrow()
  kernel.free()
})
