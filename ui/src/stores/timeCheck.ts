import { create } from 'zustand'
import { getTimeCheck, type TimeCheckEnvelope, type TimeCheckSnapshot } from '@/lib/api/timeCheck'

interface State {
  snapshot: TimeCheckSnapshot | null
  cursor: { stream_id: string; version: number } | null
  connected: boolean
  error: string | null
  apply: (message: TimeCheckEnvelope, full?: boolean) => void
  accept: (snapshot: TimeCheckSnapshot) => void
  disconnect: (error?: string) => void
  refresh: () => Promise<void>
}
export const useTimeCheck = create<State>((set, get) => ({
  snapshot: null, cursor: null, connected: false, error: null,
  apply: (message, full = false) => {
    const current = get().cursor
    if (!full && (!get().connected || current?.stream_id !== message.stream_id)) return
    if (current?.stream_id === message.stream_id && current.version > message.version) return
    set({ snapshot: message.data, cursor: { stream_id: message.stream_id, version: message.version }, connected: true, error: null })
  },
  accept: snapshot => {
    const current = get().cursor
    if (snapshot.stream_id && snapshot.version != null) {
      if (current?.stream_id === snapshot.stream_id && current.version > snapshot.version) return
      set({ cursor: { stream_id: snapshot.stream_id, version: snapshot.version } })
    }
    set({ snapshot, error: null })
  },
  disconnect: error => set({ connected: false, error: error ?? null }),
  refresh: async () => {
    const cursor = get().cursor
    try {
      const snapshot = await getTimeCheck()
      if (get().cursor === cursor) set({ snapshot, error: null })
    } catch (error) { set({ error: error instanceof Error ? error.message : String(error) }); throw error }
  },
}))
