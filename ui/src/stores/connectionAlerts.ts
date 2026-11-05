import { create } from 'zustand'
import type { AccountConnectionDetail } from '@/types/api'

/** 后端故障标识跨轮询稳定；旧接口的失败观察时间仅作为兼容回退。 */
export function connectionAlertId(accountId: number, detail: AccountConnectionDetail): string | null {
  if (detail.status !== 'unavailable') return null
  const id = detail.error?.connection_event_id ?? detail.last_failure?.observed_at ?? detail.observed_at
  return typeof id === 'string' ? `${accountId}:${id}` : null
}

interface State {
  accountId: number | null
  requestId: number
  seen: Set<string>
  open: (accountId: number) => void
  close: () => void
  observe: (frames: Record<number, AccountConnectionDetail>) => void
}

/** 整个应用共享一个连接弹窗，同一故障关闭后不会随状态推送重新弹出。 */
export const useConnectionAlerts = create<State>((set, get) => ({
  accountId: null, requestId: 0, seen: new Set(),
  open: accountId => set(state => ({ accountId, requestId: state.requestId + 1 })),
  close: () => set({ accountId: null }),
  observe: frames => {
    const current = get()
    if (current.accountId !== null) {
      const detail = frames[current.accountId]
      const id = detail && connectionAlertId(current.accountId, detail)
      if (id && !current.seen.has(id)) set({ seen: new Set([...current.seen, id]) })
      return
    }
    for (const [key, detail] of Object.entries(frames)) {
      const accountId = Number(key)
      const id = connectionAlertId(accountId, detail)
      if (id && !current.seen.has(id)) {
        set({ accountId, requestId: current.requestId + 1, seen: new Set([...current.seen, id]) })
        return
      }
    }
  },
}))
