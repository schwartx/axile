import { useCallback, useRef, useState } from 'react'
import { useReadWarningDelay } from '@/features/account/useReadWarningDelay'
import { getAccountAssetSnapshots, refreshAccountAssets } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'

/** 持仓页签轮询已存快照；仅点击核对时查询渠道，断线仍可查看上次观测。 */
export function usePositionRecords(accountId: number, instanceId: string, enabled: boolean, connectionReady: boolean) {
  const query = usePolling(useCallback((signal: AbortSignal) => getAccountAssetSnapshots(accountId, { limit: 1 }, signal), [accountId]), {
    queryKey: `position-records:${accountId}:${instanceId}`, enabled, retainDataWhenDisabled: true, intervalMs: 5000,
  })
  const identity = `${accountId}:${instanceId}`
  const readUnavailable = query.error != null || !connectionReady
  const updateDelayed = useReadWarningDelay(identity, readUnavailable, enabled)
  const currentIdentity = useRef(identity)
  currentIdentity.current = identity
  const [manual, setManual] = useState<{ identity: string; refreshing: boolean; error: unknown }>({ identity, refreshing: false, error: null })
  const busy = useRef(false)
  const reconcile = async () => {
    if (!connectionReady || busy.current) return
    busy.current = true
    setManual({ identity, refreshing: true, error: null })
    try {
      await refreshAccountAssets(accountId)
      if (currentIdentity.current === identity) await query.refresh()
    } catch (error) {
      if (currentIdentity.current === identity) setManual({ identity, refreshing: false, error })
    } finally {
      busy.current = false
      if (currentIdentity.current === identity) setManual(value => ({ ...value, refreshing: false }))
    }
  }
  const snapshot = query.data?.data[0] ?? null
  return { query, snapshot, reconcile, refreshing: manual.identity === identity && manual.refreshing,
    readUnavailable, updateDelayed,
    reconcileError: manual.identity === identity ? manual.error : null }
}
