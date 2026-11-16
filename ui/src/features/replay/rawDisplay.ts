import { RawReader } from './rawReader'
import type { RawManifest, RawPreview } from './raw'
import type { ReplayManifest, ReplayMessage, ReplayWindow } from './types'

type Display = { reader: RawReader; catalog: RawManifest; events: Map<string, Promise<ReplayMessage[]>> }
const displays = new Map<string, Display>()
export function closeRawDisplay(executionId: string) {
  displays.get(executionId)?.reader.close()
  displays.delete(executionId)
}
export async function rawDisplayManifest(executionId: string, signal?: AbortSignal): Promise<ReplayManifest> {
  closeRawDisplay(executionId)
  const [wasm] = await (await import('@/lib/wasmResources')).wasmResource('raw').read()
  signal?.throwIfAborted()
  const reader = new RawReader(executionId, () => {}, () => {}, wasm)
  const canceled = () => reader.close()
  signal?.addEventListener('abort', canceled, { once: true })
  try {
    const catalog = await reader.manifest()
    signal?.throwIfAborted()
    displays.set(executionId, { reader, catalog, events: new Map() })
    return {
      account_id: catalog.account_id, execution_id: catalog.execution_id, availability: catalog.recordings.length ? 'available' : 'unavailable',
      recordings: catalog.recordings.map(recording => ({ recording_id: recording.id, symbols: recording.symbols,
        symbols_complete: !recording.symbols_more, coverage: recording.start_us == null ? [] : [{ start_us: recording.start_us, end_us: recording.watermark + 1 }], gaps: [],
        context: { ...recording.context, schema_version: 2 },
      })),
    }
  } catch (cause) { reader.close(); throw cause }
  finally { signal?.removeEventListener('abort', canceled) }
}
export function displayMessages(events: RawPreview[], recording: string): ReplayMessage[] {
  let sequence = 0, time = Number.NEGATIVE_INFINITY
  return events.map(event => {
    const identity = event as RawPreview & { recording_id?: unknown; schema_version?: unknown }
    if (identity.recording_id !== recording || identity.schema_version !== 2 || !Number.isSafeInteger(event.capture_seq) || event.capture_seq <= sequence || !Number.isSafeInteger(event.time_us) || event.time_us < time || typeof event.topic !== 'string' || !event.data || typeof event.data !== 'object' || Array.isArray(event.data)) throw Error('REPLAY_DISPLAY_EVENT')
    sequence = event.capture_seq; time = event.time_us
    return { ...event, recording_id: recording, schema_version: 2 }
  })
}
export async function rawDisplayWindow(executionId: string, request: { recording_id: string; start_us: number; end_us: number; symbols?: string[] }, signal?: AbortSignal): Promise<ReplayWindow> {
  const display = displays.get(executionId)
  const recording = display?.catalog.recordings.find(row => row.id === request.recording_id)
  if (!display || !recording) throw Error('请刷新回放目录后重试')
  const key = JSON.stringify([recording.id, request.symbols])
  let loading = display.events.get(key)
  if (!loading) {
    loading = display.reader.displayWindow(recording, signal, request.symbols).then(events => displayMessages(events, recording.id))
    display.events.set(key, loading)
    void loading.catch(() => { if (display.events.get(key) === loading) display.events.delete(key) })
  }
  const messages = await loading
  signal?.throwIfAborted()
  // This is a complete recording from its first observation, not an invented window baseline.
  if (recording.start_us != null && request.start_us > recording.start_us) throw Error('请载入完整录制段后缩放图表')
  return { recording_id: recording.id, window_id: recording.commit_id, state_before_start: { schema_version: 2, recording_id: recording.id, time_us: recording.start_us ?? 0, state_through_seq: 0 },
    messages: messages.filter(event => event.time_us < request.end_us), next_cursor: null, complete: true,
    coverage: recording.start_us == null ? [] : [{ start_us: recording.start_us, end_us: recording.watermark + 1 }], gaps: [], context: { ...recording.context, schema_version: 2 } }
}
