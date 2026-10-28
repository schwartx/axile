import { ApiError } from '@/lib/api/client'
import { getAnalysisChart, getAnalysisStatus, refreshPerformance, type AnalysisDisplay, type AnalysisMetadata, type PerformanceRange } from '@/lib/api/performance'
import { decodeAnalysisChart } from '@/features/history/analysisClient'
import { pauseBrowserAnalysis } from '@/features/history/browserAnalysis'

export interface AnalysisState {
  metadata: AnalysisMetadata | null
  displayed: AnalysisDisplay | null
  statusError: Error | null
  resultError: Error | null
  refreshError: Error | null
  checking: boolean
  loadingResult: boolean
}
interface Entry {
  state: AnalysisState
  listeners: Set<() => void>
  flight?: Promise<boolean>
  resultFlight?: Promise<void>
  failedResultId?: string
  epoch: number
  recheck: boolean
  released?: boolean
}
const entries = new Map<string, Entry>()
const statusFlights = new Map<number, Promise<AnalysisMetadata>>()
const refreshes = new Map<number, Promise<boolean>>()
const keyOf = (id: number, range: PerformanceRange) => `${id}:${range}`
const errorOf = (error: unknown) => error instanceof Error ? error : new Error(String(error))
const readableId = (metadata: AnalysisMetadata | null) => metadata && metadata.read_view_schema_version >= 1 ? metadata.snapshot_id : null

export function performanceEntry(id: number, range: PerformanceRange): Entry {
  const key = keyOf(id, range)
  let entry = entries.get(key)
  if (!entry) {
    entry = { state: { metadata: null, displayed: null, statusError: null, resultError: null, refreshError: null, checking: false, loadingResult: false }, listeners: new Set(), epoch: 0, recheck: false }
    entries.set(key, entry)
  }
  return entry
}
function discardReleased(entry: Entry) {
  if (!entry.released || entry.listeners.size || entry.flight || entry.resultFlight) return
  for (const [key, candidate] of entries) if (candidate === entry) {
    entries.delete(key)
    const accountId = Number(key.split(':')[0])
    if (![...entries].some(([k, e]) => k.startsWith(`${accountId}:`) && e.listeners.size)) pauseBrowserAnalysis(accountId)
  }
}
/** Unmounted charts are discarded; reopening reads the durable SQLite result. */
export function releasePerformance(entry: Entry) {
  entry.released = true
  queueMicrotask(() => discardReleased(entry))
}
function publish(entry: Entry, patch: Partial<AnalysisState>) {
  entry.state = { ...entry.state, ...patch }
  entry.listeners.forEach(listener => listener())
}
function readStatus(id: number): Promise<AnalysisMetadata> {
  const existing = statusFlights.get(id)
  if (existing) return existing
  const flight = getAnalysisStatus(id).then(metadata => {
    if (metadata.schema_version !== 1) throw new Error('绩效状态版本无效')
    return metadata
  }).finally(() => { if (statusFlights.get(id) === flight) statusFlights.delete(id) })
  statusFlights.set(id, flight)
  return flight
}
function checkMetadata(id: number, range: PerformanceRange, entry: Entry): Promise<boolean> {
  if (entry.flight) return entry.flight
  const epoch = entry.epoch
  publish(entry, { checking: true })
  entry.flight = (async () => {
    try {
      const metadata = await readStatus(id)
      if (entry.epoch !== epoch) return false
      publish(entry, { metadata, statusError: null })
      return true
    } catch (error) {
      if (entry.epoch === epoch) publish(entry, { statusError: errorOf(error) })
      return false
    } finally {
      entry.flight = undefined
      publish(entry, { checking: false })
      if (entry.recheck) {
        entry.recheck = false
        if (entry.listeners.size) void checkPerformance(id, range, false)
      }
      discardReleased(entry)
    }
  })()
  return entry.flight
}
async function readChart(id: number, snapshotId: string, range: PerformanceRange): Promise<AnalysisDisplay> {
  const response = await getAnalysisChart(id, snapshotId, range)
  if (response.snapshot_id !== snapshotId || response.account_id !== id || response.range !== range) throw new Error('绩效图表版本或账户不匹配')
  return decodeAnalysisChart(response)
}
function requestResult(id: number, range: PerformanceRange, entry: Entry, retryResult: boolean) {
  const snapshotId = readableId(entry.state.metadata)
  if (!snapshotId || entry.state.displayed?.snapshot_id === snapshotId || entry.resultFlight) return
  if (!retryResult && entry.failedResultId === snapshotId) return
  publish(entry, { loadingResult: true, resultError: null })
  let expired = false
  entry.resultFlight = (async () => {
    try {
      const displayed = await readChart(id, snapshotId, range)
      if (readableId(entry.state.metadata) !== snapshotId) return
      entry.failedResultId = undefined
      publish(entry, { displayed, resultError: null })
    } catch (error) {
      if (readableId(entry.state.metadata) !== snapshotId) return
      entry.failedResultId = snapshotId
      publish(entry, { resultError: errorOf(error) })
      expired = error instanceof ApiError && error.status === 410
      // One immediate status read. A replacement chart waits for the next check.
      if (expired) await checkMetadata(id, range, entry)
    } finally {
      entry.resultFlight = undefined
      publish(entry, { loadingResult: false })
      if (!expired && readableId(entry.state.metadata) !== snapshotId) requestResult(id, range, entry, false)
      discardReleased(entry)
    }
  })()
}
/** Check small state; chart loading has an independent flight and does not delay polling. */
export function checkPerformance(id: number, range: PerformanceRange, retryResult = true): Promise<void> {
  const entry = performanceEntry(id, range)
  return checkMetadata(id, range, entry).then(success => { if (success) requestResult(id, range, entry, retryResult) })
}
export function requestPerformanceRefresh(id: number): Promise<boolean> {
  const existing = refreshes.get(id)
  if (existing) return existing
  let matching = [...entries].filter(([key]) => key.startsWith(`${id}:`))
  for (const [, entry] of matching) entry.epoch++
  const flight = (async () => {
    try {
      await refreshPerformance(id)
      // A range can mount while the mutation is pending. Observe it after POST too.
      matching = [...entries].filter(([key]) => key.startsWith(`${id}:`))
      for (const [, entry] of matching) entry.epoch++
      statusFlights.delete(id)
      await Promise.all(matching.map(([, entry]) => entry.flight))
      const checks = matching.map(([key, entry]) => {
        const metadata = entry.state.metadata
        publish(entry, { refreshError: null, ...(metadata ? { metadata: { ...metadata, status: entry.state.displayed ? 'stale' : 'pending', error: null } } : {}) })
        return entry.listeners.size ? checkPerformance(id, key.split(':')[1] as PerformanceRange) : undefined
      })
      await Promise.all(checks)
      return true
    } catch (error) {
      for (const [, entry] of matching) publish(entry, { refreshError: errorOf(error) })
      return false
    } finally { refreshes.delete(id) }
  })()
  refreshes.set(id, flight)
  return flight
}
export const snapshotPending = (metadata: Pick<AnalysisMetadata, 'status' | 'retry_at'> | null) => metadata?.status === 'pending' || metadata?.status === 'stale' || (metadata?.status === 'failed' && metadata.retry_at != null)
export function shouldPollPerformance(entry: Entry): boolean {
  const target = readableId(entry.state.metadata)
  return snapshotPending(entry.state.metadata) || entry.state.loadingResult || !!(target && target !== entry.state.displayed?.snapshot_id)
}
/** Dashboard hints invalidate an in-flight status observation and schedule a fresh one. */
export function reconcilePerformanceVersion(id: number, sourceVersion: number | null) {
  for (const [key, entry] of entries) {
    if (!key.startsWith(`${id}:`) || !entry.listeners.size || entry.state.metadata?.source_version === sourceVersion) continue
    entry.epoch++
    if (entry.flight) entry.recheck = true
    else void checkPerformance(id, key.split(':')[1] as PerformanceRange, false)
  }
}
