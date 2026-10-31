import { useEffect, useRef, useState } from 'react'
import { readReplayWindow, type ReplayWindowRequest } from '@/lib/api/replay'
import type { ReplayWindow } from '@/features/replay/types'

/** 缓存只保留两个固定窗口；旧请求即使迟到也不能修改当前录制段。 */
export function useReplayWindow(executionId: string, request: ReplayWindowRequest | null, revision = 0) {
  const [state, setState] = useState<{ key: string; value: ReplayWindow | null; error: Error | null; loading: boolean }>({ key: '', value: null, error: null, loading: false })
  const generation = useRef(0)
  const cache = useRef(new Map<string, ReplayWindow>())
  const key = request ? JSON.stringify([executionId, request, revision]) : ''
  useEffect(() => {
    const current = ++generation.current
    const controller = new AbortController()
    let active = true
    setState({ key, value: null, error: null, loading: Boolean(request) })
    if (!request) return () => controller.abort()
    const saved = cache.current.get(key)
    if (saved) { setState({ key, value: saved, error: null, loading: false }); return () => controller.abort() }
    const timeout = window.setTimeout(() => controller.abort(new Error('读取回放超时，请缩小窗口后重试')), 25_000)
    void readReplayWindow(executionId, request, controller.signal).then(result => {
      if (!active || generation.current !== current || controller.signal.aborted) return
      cache.current.set(key, result)
      while (cache.current.size > 2) cache.current.delete(cache.current.keys().next().value!)
      setState({ key, value: result, error: null, loading: false })
    }).catch(cause => {
      if (active && generation.current === current && !controller.signal.aborted) setState({ key, value: null, error: cause instanceof Error ? cause : new Error(String(cause)), loading: false })
      else if (active && generation.current === current && controller.signal.reason instanceof Error && controller.signal.reason.name !== 'AbortError') setState({ key, value: null, error: controller.signal.reason, loading: false })
    }).finally(() => {
      window.clearTimeout(timeout)
    })
    return () => { active = false; controller.abort(); window.clearTimeout(timeout) }
    // The key freezes the complete request, including recording and symbol selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return state.key === key ? state : { value: null, error: null, loading: Boolean(request) }
}
