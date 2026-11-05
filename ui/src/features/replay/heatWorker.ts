import type { HeatQuery, HeatResult } from './heatData'

/** Never queue intermediate viewports behind a running synchronous WASM call. */
export function replayHeatWorker(worker: Worker, result: (query: HeatQuery, value: HeatResult) => void, error: (message: string) => void) {
  let active = true, ready = false, running = false, sequence = 0
  let latest: HeatQuery | null = null, pending: HeatQuery | null = null, sent: HeatQuery | null = null
  const send = () => {
    if (!active || !ready || running || !pending) return
    sent = pending; pending = null; running = true
    worker.postMessage({ kind: 'query', id: ++sequence, query: sent })
  }
  const close = () => { active = false; pending = null; worker.terminate() }
  const fail = (message: string) => { if (active) { close(); error(message) } }
  worker.onmessage = event => {
    if (!active) return
    const message = event.data
    if (message.kind === 'error') { fail(message.message); return }
    if (message.kind === 'ready') ready = true
    if (message.kind === 'result' && message.id === sequence) {
      running = false
      if (sent === latest) result(sent!, message.value)
    }
    send()
  }
  worker.onerror = () => fail('历史深度计算失败，请重新打开回放')
  worker.onmessageerror = () => fail('历史深度计算失败，请重新打开回放')
  return { request(query: HeatQuery) { latest = pending = query; send() }, close }
}
