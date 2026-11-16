import init, { AccountAnalysis } from '@/features/replay/heat-wasm/axile_replay_heat'

let kernel: AccountAnalysis | null = null
self.onmessage = async (event: MessageEvent<{ kind: 'load'; input: string; wasm: ArrayBuffer } | { kind: 'query'; id: number; query: unknown }>) => {
  const request = event.data
  try {
    if (request.kind === 'load') {
      if (!(request.wasm instanceof ArrayBuffer)) throw Error('请先下载本地分析组件')
      await init({ module_or_path: request.wasm })
      kernel?.free()
      kernel = new AccountAnalysis(request.input)
      self.postMessage({ kind: 'ready' })
    } else {
      if (!kernel) throw new Error('本地分析输入尚未就绪')
      self.postMessage({ kind: 'result', id: request.id, value: JSON.parse(kernel.query(JSON.stringify(request.query))) })
    }
  } catch (error) { self.postMessage({ kind: 'error', message: error instanceof Error ? error.message : String(error) }) }
}
