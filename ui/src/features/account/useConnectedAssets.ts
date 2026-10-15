import { useEffect, useRef, useState } from 'react'
import { refreshAccountAssets } from '@/lib/api/accounts'
import { useAccountConnection } from '@/stores/liveConnection'

const requests = new Map<number, Promise<unknown>>()

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
    void request.then(() => {
      if (active) saved.current()
    }).catch(error => {
      if (active) setState({ accountId, refreshing: false, error })
    }).finally(() => {
      if (active) setState(previous => ({ ...previous, refreshing: false }))
    })
    return () => { active = false }
  }, [accountId, ready])
  return state.accountId === accountId && ready ? state : { accountId, refreshing: false, error: null }
}
