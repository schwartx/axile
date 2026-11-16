import { apiGet } from '@/lib/api/client'
import type { ReplayManifest, ReplayRange, ReplayRecording, ReplayWindow } from '@/features/replay/types'

export interface ReplayStatus {
  desired: boolean
  state: 'disabled' | 'waiting_connection' | 'starting' | 'recording' | 'closing' | 'error' | 'unsupported'
  config_version: number
  effective_version?: number | null
  recording_id?: string | null
  reason?: string | null
  error?: string | null
}

export interface ReplayWindowRequest extends ReplayRange {
  recording_id: string
  symbols?: string[]
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('回放响应结构无效')
  return value as Record<string, unknown>
}

function ranges(value: unknown): boolean {
  return Array.isArray(value) && value.every(item => {
    const row = object(item)
    return Number.isSafeInteger(row.start_us) && Number.isSafeInteger(row.end_us) && Number(row.end_us) > Number(row.start_us)
  })
}

export function validateReplayManifest(value: unknown): ReplayManifest {
  const row = object(value)
  if (!Number.isSafeInteger(row.account_id) || typeof row.execution_id !== 'string' || !Array.isArray(row.recordings)) throw new Error('回放目录缺少账户或录制段身份')
  if (row.execution_start_us != null && (!Number.isSafeInteger(row.execution_start_us) || Number(row.execution_start_us) < 0)) throw new Error('执行开始时间无效')
  if (row.execution_end_us != null && (!Number.isSafeInteger(row.execution_end_us) || Number(row.execution_end_us) < 0)) throw new Error('执行结束时间无效')
  for (const item of row.recordings) {
    const recording = object(item)
    if (typeof recording.recording_id !== 'string' || !ranges(recording.coverage)) throw new Error('回放目录包含无效覆盖范围')
    if (recording.symbols !== undefined && (!Array.isArray(recording.symbols) || recording.symbols.some(symbol => typeof symbol !== 'string' || !symbol || symbol.length > 256 || [...symbol].some(character => character < ' ')))) throw new Error('回放目录包含无效品种目录')
    if (recording.symbols_complete !== undefined && typeof recording.symbols_complete !== 'boolean') throw new Error('回放品种目录完整性无效')
    if (recording.symbols_complete === true && recording.symbols === undefined) throw new Error('完整品种目录缺少名称列表')
    const context = object(recording.context)
    if (context.schema_version !== 1 && context.schema_version !== 2) throw new Error('该录制版本暂不支持，请升级后查看')
  }
  return value as ReplayManifest
}

export function getReplayStatus(accountId: number, signal?: AbortSignal): Promise<ReplayStatus> {
  return apiGet(`/account/${accountId}/replay/status`, signal)
}

export async function getReplayManifest(executionId: string, signal?: AbortSignal): Promise<ReplayManifest> {
  return validateReplayManifest(await (await import('@/features/replay/rawDisplay')).rawDisplayManifest(executionId, signal))
}

/** 直接通过 V2 原始录制读取固定窗口。 */
export async function readReplayWindow(executionId: string, request: ReplayWindowRequest, signal?: AbortSignal): Promise<ReplayWindow> {
  if (!request.recording_id || !Number.isSafeInteger(request.start_us) || !Number.isSafeInteger(request.end_us) || request.end_us <= request.start_us) {
    throw new Error('回放窗口需要录制段和有效的整数微秒范围')
  }
  return (await import('@/features/replay/rawDisplay')).rawDisplayWindow(executionId, request, signal)
}

/** 按原录制段分别读取，保留每一段自己的基线和事件序号。 */
export async function readReplayExecution(executionId: string, segments: readonly { recording: ReplayRecording; range: ReplayRange }[], symbol: string, signal?: AbortSignal): Promise<{ range: ReplayRange; window: ReplayWindow }[]> {
  const result: { range: ReplayRange; window: ReplayWindow }[] = []
  for (const { recording, range } of segments) {
    signal?.throwIfAborted()
    const request: ReplayWindowRequest = { ...range, recording_id: recording.recording_id,
      ...(recording.symbols_complete === true && symbol ? { symbols: [symbol] } : {}) }
    result.push({ range, window: await readReplayWindow(executionId, request, signal) })
  }
  return result
}
