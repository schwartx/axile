import { useCallback, useEffect, useRef, useState } from 'react'
import type { AccountAssetSnapshot } from '@/types/api'
import { refreshAccountAssets } from '@/lib/api/accounts'
import { useAccountConnection } from '@/stores/liveConnection'

const requests = new Map<number, Promise<AccountAssetSnapshot>>()

/** 会话就绪后刷新一次资产；断开时只保留已存快照，不发起连接或查询。 */
export function useConnectedAssets(accountId: number, onSaved: () => void) {
  const ready = useAccountConnection(accountId).status === 'ready'
  const saved = useRef(onSaved)
  saved.current = onSaved
  const [state, setState] = useState<{ accountId: number; refreshing: boolean; error: unknown }>({ accountId, refreshing: false, error: null })
  useEffect(() => {
    if (!ready) return
    let active = true
    setState({ accountId, refreshing: true, error: null })
    let request = requests.get(accountId)
    if (!request) {
      request = refreshAccountAssets(accountId).finally(() => { requests.delete(accountId) })
      requests.set(accountId, request)
    }
    void request.then(result => {
      if (active) {
        saved.current()
        if (result.cash_flow_sync === 'failed') setState({ accountId, refreshing: false, error: new Error('资产已刷新，出入金流水同步失败，请在出入金区域重试同步') })
      }
    }).catch(error => {
      if (active) setState({ accountId, refreshing: false, error })
    }).finally(() => {
      if (active) setState(previous => ({ ...previous, refreshing: false }))
    })
    return () => { active = false }
  }, [accountId, ready])
  const clearError = useCallback(() => setState(previous => previous.accountId === accountId ? { ...previous, error: null } : previous), [accountId])
  return { ...(state.accountId === accountId && ready ? state : { accountId, refreshing: false, error: null }), clearError }
}
