import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { checkPerformance, releasePerformance, performanceEntry, requestPerformanceRefresh, shouldPollPerformance } from '@/features/history/performanceCache'
import type { PerformanceRange } from '@/lib/api/performance'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

export function usePerformanceSnapshot(id: number, range: PerformanceRange) {
  useReadRecovery(() => checkPerformance(id, range))
  const entry = performanceEntry(id, range)
  const subscribe = useCallback((listener: () => void) => { entry.released = false; entry.listeners.add(listener); return () => { entry.listeners.delete(listener); releasePerformance(entry) } }, [entry])
  const state = useSyncExternalStore(subscribe, () => entry.state)
  useEffect(() => {
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const schedule = () => {
      clearTimeout(timer)
      if (!disposed && !document.hidden && shouldPollPerformance(entry)) timer = setTimeout(() => { void check(false) }, 2000)
    }
    const check = async (retryResult: boolean) => {
      clearTimeout(timer)
      if (disposed || document.hidden) return
      await checkPerformance(id, range, retryResult)
      if (disposed || document.hidden) return
      if (entry.state.metadata?.status === 'empty' && !entry.state.statusError && !entry.state.refreshError) await requestPerformanceRefresh(id)
      schedule()
    }
    const visibility = () => { clearTimeout(timer); if (!document.hidden) void check(true) }
    document.addEventListener('visibilitychange', visibility)
    entry.listeners.add(schedule)
    void check(true)
    return () => { disposed = true; clearTimeout(timer); entry.listeners.delete(schedule); document.removeEventListener('visibilitychange', visibility); releasePerformance(entry) }
  }, [id, range, entry])
  return { ...state, refresh: useCallback(() => requestPerformanceRefresh(id), [id]), check: useCallback(() => checkPerformance(id, range), [id, range]) }
}

export const useAnalysisSnapshot = usePerformanceSnapshot
