import { create } from 'zustand'
import { operateAccountConnection } from '@/lib/api/accounts'
import { useLiveConnection } from '@/stores/liveConnection'

type Action = 'reconnect' | 'disconnect'
interface Operation { pending: Action | null; error: unknown }
interface State {
  detailOpen: Record<number, boolean>
  setDetailOpen: (accountId: number, open: boolean) => void
  operations: Record<number, Operation>
  run: (accountId: number, action: Action) => Promise<void>
}
const idle: Operation = { pending: null, error: null }

/** 列表、详情和弹窗共享在途操作，避免快速点击多个入口重复提交。 */
export const useConnectionOperations = create<State>((set, get) => ({
  operations: {},
  detailOpen: {},
  setDetailOpen: (accountId, open) => set(state => ({ detailOpen: { ...state.detailOpen, [accountId]: open } })),
  run: async (accountId, action) => {
    const live = useLiveConnection.getState()
    const status = live.frames[accountId]?.status
    if (!live.connected || get().operations[accountId]?.pending || status === 'connecting' || status === 'recovering') return
    const update = (operation: Operation) => set(state => ({ operations: { ...state.operations, [accountId]: operation } }))
    update({ pending: action, error: null })
    try {
      await operateAccountConnection(accountId, action)
      update(idle)
    } catch (error) {
      // 只展示本次错误，状态仍由 SSE 更新；不重放请求，也不另起只读查询。
      update({ pending: null, error })
    }
  },
}))

export function useConnectionOperation(accountId: number) {
  return useConnectionOperations(state => state.operations[accountId] ?? idle)
}
