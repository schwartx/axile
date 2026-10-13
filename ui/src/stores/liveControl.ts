import { create } from 'zustand'

export interface ControlRule {
  id: string
  operation: string
  scope: 'account' | 'symbol' | 'group'
  symbol: string | null
  rule: 'per_day' | 'per_minute' | 'min_interval_ms'
  value: number | null
  limit: number | null
  remaining: number | null
  estimated_wait_ms: number
  on_trigger: string | null
  label?: string
}
export interface ControlFrame {
  account_id: number
  execution_id: string | null
  instance_id: string | null
  status: string
  seq: number
  sampled_at_ms: number | null
  stale: boolean
  incomplete: boolean
  pending_policy: boolean
  timezone: string | null
  control_date: string | null
  actual_policy: unknown
  saved_policy: unknown
  rules: ControlRule[]
  metrics: ControlRule[]
  waiting_total: number
  waiting: { id: number; operation: string; symbol: string | null; priority: number; waited_ms: number; kind: string; estimated_wait_ms?: number; hits: Record<string, unknown>[] }[]
  records: { event_uid: string; occurred_at_ms: number; operation: string; symbol: string | null; decision: string; outcome: string; metadata: Record<string, unknown> }[]
}
export interface ControlEnvelope {
  stream_id: string
  version: number
  kind: 'snapshot' | 'update'
  data: ControlFrame[]
  removed?: number[]
  freshness?: { account_id: number; sampled_at_ms: number | null; stale: boolean }[]
}
interface State {
  frames: Record<number, ControlFrame>
  streamId: string | null
  version: number
  connected: boolean
  lastReceivedAt: number | null
  apply: (message: ControlEnvelope) => void
  disconnect: () => void
  expire: (now: number) => void
}
export const useLiveControl = create<State>((set) => ({
  frames: {}, streamId: null, version: -1, connected: false, lastReceivedAt: null,
  apply: (message) => set((state) => {
    if (message.kind !== 'snapshot' && message.stream_id !== state.streamId) return state
    if (message.stream_id === state.streamId && (message.version < state.version || (message.version === state.version && (message.kind !== 'snapshot' || state.connected)))) return state
    const frames = message.kind === 'snapshot' ? {} as Record<number, ControlFrame> : { ...state.frames }
    for (const frame of message.data) frames[frame.account_id] = frame
    for (const id of message.removed ?? []) delete frames[id]
    for (const fresh of message.freshness ?? []) {
      if (frames[fresh.account_id]) frames[fresh.account_id] = { ...frames[fresh.account_id], sampled_at_ms: fresh.sampled_at_ms, stale: fresh.stale }
    }
    return { frames, streamId: message.stream_id, version: message.version, connected: true, lastReceivedAt: Date.now() }
  }),
  disconnect: () => set((state) => ({ connected: false, frames: Object.fromEntries(Object.entries(state.frames).map(([id, frame]) => [id, { ...frame, stale: true }])) })),
  expire: (now) => set((state) => {
    let changed = false
    const frames = { ...state.frames }
    for (const frame of Object.values(frames)) {
      if (!frame.stale && (!state.lastReceivedAt || now - state.lastReceivedAt > 5000)) {
        frames[frame.account_id] = { ...frame, stale: true }; changed = true
      }
    }
    return changed ? { frames } : state
  }),
}))
