import { useLayoutEffect, useRef, useState } from 'react'
import { replayHeatWorker } from './heatWorker'
import { packHeat, type HeatSource, type HeatQuery, type HeatResult } from './heatData'

/** One running query and one latest pending viewport; data changes terminate the old worker. */
export function useReplayHeat(sources: HeatSource[], query: HeatQuery | null) {
  const [result, setResult] = useState<{ sources: HeatSource[]; query: HeatQuery; value: HeatResult } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const request = useRef<(query: HeatQuery) => void>(() => {})
  const current = useRef(query)
  current.current = query
  useLayoutEffect(() => {
    setError(null); setResult(null)
    let worker: Worker
    try { worker = new Worker(new URL('./heat.worker.ts', import.meta.url), { type: 'module' }) }
    catch { setError('历史深度计算无法启动，请检查浏览器设置'); return }
    const client = replayHeatWorker(worker, (query, value) => setResult({ sources, query, value }), setError)
    request.current = client.request
    if (current.current) client.request(current.current)
    const rows = packHeat(sources)
    worker.postMessage({ kind: 'load', rows }, rows.map(row => row.buffer))
    return () => { client.close(); request.current = () => {} }
  }, [sources])
  useLayoutEffect(() => { if (query) request.current(query) }, [query])
  const frame = result?.sources === sources ? result : null
  return { frame, error, pending: query != null && frame?.query !== query }
}
