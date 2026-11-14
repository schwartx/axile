/** Never queue intermediate viewports behind a running synchronous WASM call. */
export function analysisWorker<Q, R>(worker: Worker, result: (query: Q, value: R) => void, error: (message: string) => void, failure = '本地分析失败，请重新加载数据') {
  let active = true, ready = false, running = false, sequence = 0
  let latest: Q | null = null, pending: Q | null = null, sent: Q | null = null
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
  worker.onerror = () => fail(failure)
  worker.onmessageerror = () => fail(failure)
  return { request(query: Q) { latest = pending = query; send() }, close }
}
