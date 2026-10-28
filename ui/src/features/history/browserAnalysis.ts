import { ApiError } from '@/lib/api/client'

let worker: Worker | undefined
let sequence = 0
const pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>()

export function pauseBrowserAnalysis(accountId: number) {
  worker?.postMessage({ id: 0, accountId, command: 'pause', query: {} })
}

function analysisWorker() {
  if (!worker) {
    worker = new Worker(new URL('./analysis.worker.ts', import.meta.url), { type: 'module' })
    worker.onmessage = ({ data }) => {
      const request = pending.get(data.id)
      if (!request) return
      pending.delete(data.id)
      if (data.error) request.reject(new ApiError(data.error.status ?? 503, data.error.message, { code: data.error.code }))
      else request.resolve(data.value)
    }
    worker.onerror = () => {
      worker?.terminate()
      worker = undefined
      for (const request of pending.values()) request.reject(new Error('浏览器分析进程已停止，请重试'))
      pending.clear()
    }
  }
  return worker
}

/** Only active requests and the displayed chart live in memory; SQLite owns reuse. */
export function browserAnalysis<T>(accountId: number, command: string, query: object = {}, signal?: AbortSignal): Promise<T> {
  signal?.throwIfAborted()
  if (!Number.isSafeInteger(accountId) || accountId <= 0) return Promise.reject(new Error('账户编号无效'))
  const id = ++sequence
  const activeWorker = analysisWorker()
  return new Promise<T>((resolve, reject) => {
    const abort = () => { pending.delete(id); reject(signal?.reason ?? new DOMException('Aborted', 'AbortError')) }
    const cleanup = () => signal?.removeEventListener('abort', abort)
    pending.set(id, {
      resolve: value => { cleanup(); resolve(value as T) },
      reject: error => { cleanup(); reject(error) },
    })
    signal?.addEventListener('abort', abort, { once: true })
    activeWorker.postMessage({ id, accountId, command, query })
  })
}
