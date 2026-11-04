import init, { Heat } from './heat-wasm/axile_replay_heat'
import wasmUrl from './heat-wasm/axile_replay_heat_bg.wasm?url'
import { heatResult, type HeatQuery } from './heatData'

let kernels: Heat[] = []
const ready = init({ module_or_path: wasmUrl })
self.onmessage = async (event: MessageEvent<{ kind: 'load'; rows: Float64Array[] } | { kind: 'query'; id: number; query: HeatQuery }>) => {
  const request = event.data
  try {
    await ready
    if (request.kind === 'load') {
      kernels.forEach(kernel => kernel.free())
      kernels = request.rows.map(rows => new Heat(rows))
      self.postMessage({ kind: 'ready' })
      return
    }
    const { range, view, layout } = request.query
    const parts = kernels.map(kernel => kernel.query(range.start_us, range.end_us, view.min, view.max, view.tick, layout.left, layout.right, layout.top, layout.bottom))
    const data = new Float64Array(parts.reduce((length, part) => length + part.length, 0))
    let offset = 0
    for (const part of parts) { data.set(part, offset); offset += part.length }
    self.postMessage({ kind: 'result', id: request.id, value: heatResult(data, request.query) }, { transfer: [data.buffer] })
  } catch { self.postMessage({ kind: 'error', message: '历史深度计算失败，请重新打开回放' }) }
}
