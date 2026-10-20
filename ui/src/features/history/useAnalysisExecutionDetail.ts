import { useCallback, useEffect, useMemo, useSyncExternalStore } from 'react'
import { EMPTY_DETAIL_STATE, executionDetailEntry, loadExecutionDetail } from '@/features/history/analysisExecutionDetail'

export function useAnalysisExecutionDetail(accountId: number, snapshotId: string, recordId: number | null) {
  const entry = useMemo(() => recordId == null ? null : executionDetailEntry(accountId, snapshotId, recordId), [accountId, snapshotId, recordId])
  const subscribe = useCallback((listener: () => void) => {
    if (!entry) return () => {}
    entry.listeners.add(listener)
    return () => { entry.listeners.delete(listener); if (!entry.listeners.size) entry.controller?.abort() }
  }, [entry])
  const read = useCallback(() => entry?.state ?? EMPTY_DETAIL_STATE, [entry])
  const state = useSyncExternalStore(subscribe, read, read)
  useEffect(() => { if (entry) void loadExecutionDetail(entry) }, [entry])
  return { ...state,
    retry: useCallback(() => entry ? loadExecutionDetail(entry, false, true) : Promise.resolve(), [entry]),
    loadMore: useCallback(() => entry ? loadExecutionDetail(entry, true) : Promise.resolve(), [entry]),
  }
}
