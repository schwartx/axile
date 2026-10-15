import { create } from 'zustand'
import type { CurrentOrdersSnapshot } from '@/lib/api/orders'

export interface OrdersFrame { account_id: number; snapshot: CurrentOrdersSnapshot }
export interface OrdersEnvelope {
  stream_id: string
  version: number
  kind: 'snapshot' | 'update'
  data: OrdersFrame[]
  removed?: number[]
}
interface State {
  frames: Record<number, CurrentOrdersSnapshot>
  streamId: string | null
  version: number
  connected: boolean
  apply: (message: OrdersEnvelope) => void
  applyRead: (accountId: number, snapshot: CurrentOrdersSnapshot) => void
  disconnect: () => void
}

/** 委托随账户连接维护；页面只订阅快照，显式核对也不能证明 SSE 健康。 */
export const useLiveOrders = create<State>((set) => ({
  frames: {}, streamId: null, version: -1, connected: false,
  apply: (message) => set((state) => {
    if (message.kind !== 'snapshot' && (!state.connected || message.stream_id !== state.streamId)) return state
    if (message.stream_id === state.streamId && (message.version < state.version
      || (message.version === state.version && (message.kind !== 'snapshot' || state.connected)))) return state
    const frames = message.kind === 'snapshot' ? {} : { ...state.frames }
    for (const frame of message.data) {
      const previous = state.frames[frame.account_id]
      const older = message.stream_id === state.streamId && previous?.instance_id === frame.snapshot.instance_id
        && (previous?.version ?? 0) > (frame.snapshot.version ?? 0)
      frames[frame.account_id] = older ? previous : frame.snapshot
    }
    for (const id of message.removed ?? []) delete frames[id]
    return { frames, streamId: message.stream_id, version: message.version, connected: true }
  }),
  applyRead: (accountId, snapshot) => set(state => {
    const previous = state.frames[accountId]
    if (previous && ((previous.instance_id && previous.instance_id !== snapshot.instance_id)
      || (previous.version ?? 0) > (snapshot.version ?? 0))) return state
    return { frames: { ...state.frames, [accountId]: snapshot } }
  }),
  disconnect: () => set({ connected: false }),
}))
