import { useMemo } from 'react'
import { create } from 'zustand'
import type { AccountConnectionDetail } from '@/types/api'

export interface ConnectionFrame {
  account_id: number
  connection: AccountConnectionDetail
}
export interface ConnectionEnvelope {
  stream_id: string
  version: number
  kind: 'snapshot' | 'update'
  data: ConnectionFrame[]
  removed?: number[]
}
interface State {
  frames: Record<number, AccountConnectionDetail>
  streamId: string | null
  version: number
  connected: boolean
  apply: (message: ConnectionEnvelope) => void
  disconnect: () => void
}

/** 连接状态只由 SSE 快照与变更驱动，账户 REST 响应不能覆盖此状态。 */
export const useLiveConnection = create<State>((set) => ({
  frames: {}, streamId: null, version: -1, connected: false,
  apply: (message) => set((state) => {
    if (message.kind !== 'snapshot' && (!state.connected || message.stream_id !== state.streamId)) return state
    if (message.stream_id === state.streamId && (message.version < state.version
      || (message.version === state.version && (message.kind !== 'snapshot' || state.connected)))) return state
    const frames: State['frames'] = message.kind === 'snapshot' ? {} : { ...state.frames }
    for (const frame of message.data) frames[frame.account_id] = frame.connection
    for (const id of message.removed ?? []) delete frames[id]
    return { frames, streamId: message.stream_id, version: message.version, connected: true }
  }),
  disconnect: () => set({ connected: false }),
}))

const awaitingConnection: AccountConnectionDetail = {
  status: 'unknown', observed_at: null, message: '连接状态待同步', error: null,
}

/** 展示保留最近的 SSE 快照；不能用它判断操作是否可执行。 */
export function useAccountConnectionSnapshot(accountId: number): AccountConnectionDetail {
  return useLiveConnection(state => state.frames[accountId] ?? awaitingConnection)
}

/** 事件流尚未同步或已断开时，不沿用旧的就绪结论或当前故障建议。 */
export function useAccountConnection(accountId: number): AccountConnectionDetail {
  const frame = useLiveConnection(state => state.frames[accountId])
  const connected = useLiveConnection(state => state.connected)
  return useMemo(() => connected && frame ? frame : {
    ...frame, status: 'unknown', worker_alive: undefined, sampled_at: null, observed_at: null,
    message: '连接状态待同步', error: null, presentation: null,
  }, [frame, connected])
}
