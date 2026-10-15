import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { checkPerformance, performanceEntry, requestPerformanceRefresh, snapshotPending } from './performanceCache'
import type { PerformanceRange } from '@/lib/api/performance'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

export function usePerformanceSnapshot(id: number, range: PerformanceRange) {
  useReadRecovery(() => checkPerformance(id, range))
  const entry = performanceEntry(id, range)
  const subscribe = useCallback((listener: () => void) => { entry.listeners.add(listener); return () => { entry.listeners.delete(listener) } }, [entry])
  const state = useSyncExternalStore(subscribe, () => entry.state)
  useEffect(() => {
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const check = async () => {
      clearTimeout(timer)
      if (disposed || document.hidden) return
      await checkPerformance(id, range)
      if (disposed || document.hidden) return
      if (entry.state.data?.status === 'empty' && !entry.state.error) await requestPerformanceRefresh(id)
      if (!disposed && !document.hidden && snapshotPending(entry.state.data) && !entry.state.error) timer = setTimeout(check, 2000)
    }
    const visibility = () => { clearTimeout(timer); if (!document.hidden) void check() }
    document.addEventListener('visibilitychange', visibility)
    const unsubscribe = () => {
      clearTimeout(timer)
      if (!disposed && !document.hidden && snapshotPending(entry.state.data) && !entry.state.error) timer = setTimeout(check, 2000)
    }
    entry.listeners.add(unsubscribe)
    void check()
    return () => { disposed = true; clearTimeout(timer); entry.listeners.delete(unsubscribe); document.removeEventListener('visibilitychange', visibility) }
  }, [id, range, entry])
  return { ...state, refresh: useCallback(async () => { await requestPerformanceRefresh(id) }, [id]), check: useCallback(() => checkPerformance(id, range), [id, range]) }
}
