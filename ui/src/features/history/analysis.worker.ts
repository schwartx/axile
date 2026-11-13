import init, { AccountAnalysis, ReplayAnalysis } from '@/features/replay/heat-wasm/axile_replay_heat'
import wasmUrl from '@/features/replay/heat-wasm/axile_replay_heat_bg.wasm?url'

let kernel: AccountAnalysis | ReplayAnalysis | null = null
const ready = init({ module_or_path: wasmUrl })
self.onmessage = async (event: MessageEvent<{ kind: 'load'; mode: 'account' | 'replay'; input: string } | { kind: 'query'; id: number; query: unknown }>) => {
  const request = event.data
  try {
    await ready
    if (request.kind === 'load') {
      kernel?.free()
      kernel = request.mode === 'account' ? new AccountAnalysis(request.input) : new ReplayAnalysis(request.input)
      self.postMessage({ kind: 'ready' })
    } else {
      if (!kernel) throw new Error('本地分析输入尚未就绪')
      self.postMessage({ kind: 'result', id: request.id, value: JSON.parse(kernel.query(JSON.stringify(request.query))) })
    }
  } catch (error) { self.postMessage({ kind: 'error', message: error instanceof Error ? error.message : String(error) }) }
}
