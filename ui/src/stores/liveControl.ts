import { create } from 'zustand'

/** 监控中预警所需的策略投影；旧快照可缺少操作目录。 */
export interface ControlWarningPolicy {
  operations?: Record<string, { daily_warning_percent?: number | null }>
}

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
  actual_policy: ControlWarningPolicy | null
  saved_policy: ControlWarningPolicy | null
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
  error: string | null
  lastReceivedAt: number | null
  disconnectedAt: number | null
  apply: (message: ControlEnvelope) => void
  beginReconnect: (now?: number) => void
  disconnect: (now?: number, error?: string | null) => void
  expire: (now: number) => void
}
export const useLiveControl = create<State>((set) => ({
  frames: {}, streamId: null, version: -1, connected: false, error: null, lastReceivedAt: null, disconnectedAt: null,
  apply: (message) => set((state) => {
    if (message.kind !== 'snapshot' && message.stream_id !== state.streamId) return state
    if (message.stream_id === state.streamId && (message.version < state.version || (message.version === state.version && (message.kind !== 'snapshot' || state.connected)))) return state
    const frames = message.kind === 'snapshot' ? {} as Record<number, ControlFrame> : { ...state.frames }
    for (const frame of message.data) frames[frame.account_id] = frame
    for (const id of message.removed ?? []) delete frames[id]
    for (const fresh of message.freshness ?? []) {
      if (frames[fresh.account_id]) frames[fresh.account_id] = { ...frames[fresh.account_id], sampled_at_ms: fresh.sampled_at_ms, stale: fresh.stale }
    }
    return { frames, streamId: message.stream_id, version: message.version, connected: true, error: null, lastReceivedAt: Date.now(), disconnectedAt: null }
  }),
  // 主动重连给一次缓冲；连续报错不延长已有缓冲，也不清除服务端告警。
  beginReconnect: (now = Date.now()) => set({ connected: false, error: null, disconnectedAt: now }),
  disconnect: (now = Date.now(), error = null) => set((state) => ({
    connected: false, error, disconnectedAt: state.disconnectedAt ?? now,
    // 明确的业务域错误已确认采样不可用；短暂网络断线仍沿用原有缓冲。
    frames: error ? Object.fromEntries(Object.entries(state.frames).map(([id, frame]) => [id, { ...frame, stale: true }])) : state.frames,
  })),
  expire: (now) => set((state) => {
    const since = state.disconnectedAt ?? state.lastReceivedAt
    if (since != null && now - since <= 5000) return state
    let changed = false
    const frames = { ...state.frames }
    for (const frame of Object.values(frames)) {
      if (!frame.stale) {
        frames[frame.account_id] = { ...frame, stale: true }; changed = true
      }
    }
    return changed ? { frames } : state
  }),
}))
