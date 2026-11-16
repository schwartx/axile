import { useEffect, useRef, useState } from 'react'
import { analysisWorker } from '@/lib/analysisWorker'

/** Dataset changes terminate the old worker; only the latest query may publish. */
export function useLocalAnalysis<Q, R>(mode: 'account' | 'replay', input: object | null, query: Q | null) {
  const [result, setResult] = useState<{ input: object; query: Q; value: R } | null>(null)
  const [failure, setFailure] = useState<{ input: object; error: Error } | null>(null)
  const [initialized, setInitialized] = useState<object | null>(null)
  const current = useRef(query)
  current.current = query
  const request = useRef<(q: Q) => void>(() => {})
  useEffect(() => {
    if (!input) return
    setFailure(null); setInitialized(null)
    let client: ReturnType<typeof analysisWorker<Q, R>> | undefined
    let active = true
    const load = async () => {
      try {
        const serialized = JSON.stringify(input)
        if (new TextEncoder().encode(serialized).byteLength > 16 * 1024 * 1024) throw new Error('窗口超过 16 MiB，请缩短范围')
        const [wasm] = await (await import('@/lib/wasmResources')).wasmResource('analysis').read()
        if (!active) return
        const worker = new Worker(new URL('./analysis.worker.ts', import.meta.url), { type: 'module' })
        client = analysisWorker<Q, R>(worker, (query, value) => setResult({ input, query, value }), message => setFailure({ input, error: new Error(message) }), () => setInitialized(input))
        request.current = client.request
        worker.postMessage({ kind: 'load', mode, input: serialized, wasm }, [wasm!])
        if (current.current) client.request(current.current)
      } catch (error) {
        if (active) { client?.close(); request.current = () => {}; setFailure({ input, error: error instanceof Error ? error : new Error(String(error)) }) }
      }
    }
    void load()
    return () => { active = false; client?.close(); request.current = () => {} }
  }, [input, mode])
  useEffect(() => { if (query) request.current(query) }, [query])
  const error = input && failure?.input === input ? failure.error : null
  const value = input && result?.input === input && result.query === query && !error ? result.value : null
  return { value, error, pending: !!input && !!query && !value && !error, initializing: !!input && initialized !== input && !error }
}
