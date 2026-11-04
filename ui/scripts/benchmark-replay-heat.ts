import { readFileSync } from 'node:fs'
import init, { Heat } from '../src/features/replay/heat-wasm/axile_replay_heat'
import { packHeat } from '../src/features/replay/heatData'
import { replayHeatCells, replayChartLayout } from '../src/features/replay/chartGeometry'
import type { ReplayQuote } from '../src/features/replay/types'
await init({ module_or_path: readFileSync(new URL('../src/features/replay/heat-wasm/axile_replay_heat_bg.wasm', import.meta.url)) })
const layout = replayChartLayout(1400, 700), view = { min: 99, max: 102, tick: .01 }
const time = (fn: () => void) => { const start = performance.now(); fn(); return performance.now() - start }
for (const count of [10000, 50000, 100000]) {
  const quotes = Array.from({ length: count }, (_, i) => ({ symbol: 'X', time_us: i * 1000, capture_seq: i, book_valid: true, available: true, valid_until_us: (i + 1) * 1000,
    bids: Array.from({ length: 20 }, (_, j) => ({ price: 100 + j * .01, quantity: (i + j) % 100 })), asks: [],
  } as unknown as ReplayQuote))
  const end = count * 1000
  let kernel: Heat
  const initMs = time(() => { kernel = new Heat(packHeat([{ quotes, gaps: [], end }])[0]) })
  const js: number[] = [], wasm: number[] = []
  for (let i = 0; i < 8; i++) {
    const range = { start_us: i * end / 100, end_us: end * (.5 + i / 100) }
    const a = time(() => { replayHeatCells(quotes, range, end, view, layout, []) })
    const b = time(() => { kernel.query(range.start_us, range.end_us, view.min, view.max, view.tick, layout.left, layout.right, layout.top, layout.bottom) })
    if (i) { js.push(a); wasm.push(b) }
  }
  const median = (values: number[]) => +values.sort((a,b) => a-b)[Math.floor(values.length / 2)].toFixed(2)
  console.log(JSON.stringify({ quotes: count, levels: count * 20, initializationMs: +initMs.toFixed(2), jsMedianMs: median(js), wasmMedianMs: median(wasm) }))
  kernel!.free()
}
