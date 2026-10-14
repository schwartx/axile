import { useCallback, useEffect, useRef, useState } from 'react'
import { usePolling } from '@/lib/hooks/usePolling'
import type { TargetWeightSnapshot } from '@/types/api'

export function targetSnapshotIdentity(snapshot: TargetWeightSnapshot | null): string | null {
  if (!snapshot?.calculated_at) return null
  return [snapshot.calculated_at, snapshot.source, snapshot.execution_id, snapshot.context_account_id].join('|')
}

/** 把只读快照加载与会执行用户代码的主动重算分成两个明确动作。 */
export function useTargetSnapshot(
  reader: (signal: AbortSignal) => Promise<TargetWeightSnapshot>,
  recalculator: () => Promise<TargetWeightSnapshot>,
  queryKey: string,
  enabled = true,
) {
  const snapshot = usePolling(reader, { queryKey, intervalMs: 0, enabled })
  const reloadSnapshot = snapshot.refresh
  const [recalculating, setRecalculating] = useState(false)
  const [recalculateError, setRecalculateError] = useState<Error | null>(null)
  const currentKey = useRef(queryKey)
  currentKey.current = queryKey
  const operation = useRef({ generation: 0, busy: false })
  useEffect(() => {
    const active = operation.current
    active.generation += 1
    active.busy = false
    setRecalculating(false)
    setRecalculateError(null)
    return () => { active.generation += 1 }
  }, [queryKey, enabled])
  const snapshotIdentityRef = useRef(targetSnapshotIdentity(snapshot.data))

  useEffect(() => {
    const identity = targetSnapshotIdentity(snapshot.data)
    if (identity !== snapshotIdentityRef.current) {
      snapshotIdentityRef.current = identity
      setRecalculateError(null)
    }
  }, [snapshot.data])

  const recalculate = useCallback(async () => {
    const active = operation.current
    if (!enabled || active.busy) return
    active.busy = true
    const generation = ++active.generation
    const valid = () => generation === active.generation && currentKey.current === queryKey
    setRecalculating(true)
    setRecalculateError(null)
    try {
      await recalculator()
      if (!valid()) return
      await reloadSnapshot()
      if (valid()) setRecalculateError(null)
    } catch (error) {
      if (valid()) setRecalculateError(error instanceof Error ? error : new Error(String(error)))
    } finally {
      if (valid()) { active.busy = false; setRecalculating(false) }
    }
  }, [enabled, queryKey, recalculator, reloadSnapshot])

  return {
    ...snapshot,
    recalculating,
    recalculateError,
    recalculate,
    reloadSnapshot,
  }
}
