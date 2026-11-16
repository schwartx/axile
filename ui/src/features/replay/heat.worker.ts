import init, { Heat } from './heat-wasm/axile_replay_heat'
import { heatResult, type HeatQuery } from './heatData'

let kernels: Heat[] = []
let ready: ReturnType<typeof init>
self.onmessage = async (event: MessageEvent<{ kind: 'load'; rows: Float64Array[]; wasm: ArrayBuffer } | { kind: 'query'; id: number; query: HeatQuery }>) => {
  const request = event.data
  try {
    if (request.kind === 'load') {
      if (!(request.wasm instanceof ArrayBuffer)) throw Error('请先下载回放组件')
      ready = init({ module_or_path: request.wasm }); await ready
      kernels.forEach(kernel => kernel.free())
      kernels = request.rows.map(rows => new Heat(rows))
      self.postMessage({ kind: 'ready' })
      return
    }
    await ready
    const { range, view, layout } = request.query
    const parts = kernels.map(kernel => kernel.query(range.start_us, range.end_us, view.min, view.max, view.tick, layout.left, layout.right, layout.top, layout.bottom))
    const data = new Float64Array(parts.reduce((length, part) => length + part.length, 0))
    let offset = 0
    for (const part of parts) { data.set(part, offset); offset += part.length }
    self.postMessage({ kind: 'result', id: request.id, value: heatResult(data, request.query) }, { transfer: [data.buffer] })
  } catch { self.postMessage({ kind: 'error', message: '历史深度计算失败，请重新打开回放' }) }
}
