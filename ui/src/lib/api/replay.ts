import { apiGet } from '@/lib/api/client'
import type { ReplayManifest, ReplayMessage, ReplayRange, ReplayWindow } from '@/features/replay/types'

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

const MAX_PAGE_BYTES = 4 * 1024 * 1024
export const MAX_WINDOW_BYTES = 16 * 1024 * 1024
export const MAX_WINDOW_MESSAGES = 20_000

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

/** 边界只接受已命名的录制段与整数微秒，不能用 UTC 猜录制身份。 */
export function replayWindowQuery(request: ReplayWindowRequest, cursor?: string): string {
  if (!request.recording_id || !Number.isSafeInteger(request.start_us) || !Number.isSafeInteger(request.end_us) || request.end_us <= request.start_us) {
    throw new Error('回放窗口需要录制段和有效的整数微秒范围')
  }
  const params = new URLSearchParams({ recording_id: request.recording_id, start_us: String(request.start_us), end_us: String(request.end_us) })
  if (request.symbols?.length) params.set('symbols', request.symbols.join(','))
  if (cursor) params.set('cursor', cursor)
  return params.toString()
}

export function validateReplayManifest(value: unknown): ReplayManifest {
  const row = object(value)
  if (!Number.isSafeInteger(row.account_id) || typeof row.execution_id !== 'string' || !Array.isArray(row.recordings)) throw new Error('回放目录缺少账户或录制段身份')
  for (const item of row.recordings) {
    const recording = object(item)
    if (typeof recording.recording_id !== 'string' || !ranges(recording.coverage)) throw new Error('回放目录包含无效覆盖范围')
    const context = object(recording.context)
    if (context.schema_version !== 1) throw new Error('该录制版本暂不支持，请升级后查看')
  }
  return value as ReplayManifest
}

export function validateReplayPage(value: unknown, request: ReplayWindowRequest, windowId?: string): ReplayWindow {
  const row = object(value)
  if (row.recording_id !== request.recording_id || typeof row.window_id !== 'string' || (windowId && row.window_id !== windowId)) throw new Error('回放窗口已变化，请重新载入')
  if (!Array.isArray(row.messages) || row.messages.length > 2000 || !ranges(row.coverage) || !Array.isArray(row.gaps) || typeof row.complete !== 'boolean') throw new Error('回放分页结构或数量超出约定')
  if (row.next_cursor !== null && typeof row.next_cursor !== 'string') throw new Error('回放游标无效')
  if (object(row.context).schema_version !== 1) throw new Error('该录制版本暂不支持，请升级后查看')
  if (new TextEncoder().encode(JSON.stringify(value)).byteLength > MAX_PAGE_BYTES) throw new Error('回放单页超过 4 MiB，请缩小窗口')
  let previous = -1
  let previousTime = Number.NEGATIVE_INFINITY
  for (const raw of row.messages) {
    const message = object(raw)
    if (message.schema_version !== 1 || message.recording_id !== request.recording_id || !Number.isSafeInteger(message.capture_seq) || Number(message.capture_seq) <= previous || !Number.isSafeInteger(message.time_us)) throw new Error('回放消息身份、版本或顺序无效')
    if (Number(message.time_us) < request.start_us || Number(message.time_us) >= request.end_us || typeof message.topic !== 'string') throw new Error('回放消息超出所选窗口')
    if (Number(message.time_us) < previousTime) throw new Error('录制段内观察时间回退')
    object(message.data)
    previous = Number(message.capture_seq)
    previousTime = Number(message.time_us)
  }
  return value as ReplayWindow
}

export function getReplayStatus(accountId: number, signal?: AbortSignal): Promise<ReplayStatus> {
  return apiGet(`/account/${accountId}/replay/status`, signal)
}

export async function getReplayManifest(executionId: string, signal?: AbortSignal): Promise<ReplayManifest> {
  return validateReplayManifest(await apiGet(`/account/executions/${encodeURIComponent(executionId)}/replay`, signal))
}

/** 单个固定窗口至多保留 2 万条／16 MiB；更长录制由页面分窗读取。 */
export async function readReplayWindow(executionId: string, request: ReplayWindowRequest, signal?: AbortSignal): Promise<ReplayWindow> {
  const messages: ReplayMessage[] = []
  const cursors = new Set<string>()
  let first: ReplayWindow | null = null
  let cursor: string | undefined
  let bytes = 0
  for (let pageIndex = 0; pageIndex < 20; pageIndex++) {
    signal?.throwIfAborted()
    const raw = await apiGet<unknown>(`/account/executions/${encodeURIComponent(executionId)}/replay/window?${replayWindowQuery(request, cursor)}`, signal)
    const page = validateReplayPage(raw, request, first?.window_id)
    if (!first) {
      if (page.state_before_start == null) throw new Error('回放窗口缺少恢复基线，无法安全重建')
      first = page
    }
    bytes += new TextEncoder().encode(JSON.stringify(page)).byteLength
    if (bytes > MAX_WINDOW_BYTES || messages.length + page.messages.length > MAX_WINDOW_MESSAGES) throw new Error('该窗口事件过多，请缩小读取时段；已暂停回放')
    const last = messages.at(-1)
    if (last && page.messages[0] && page.messages[0].capture_seq <= last.capture_seq) throw new Error('回放分页重复或倒序，请重新载入')
    if (last && page.messages[0] && page.messages[0].time_us < last.time_us) throw new Error('录制段内观察时间回退')
    messages.push(...page.messages)
    if (page.complete) return { ...first, messages, next_cursor: null, complete: true }
    if (!page.next_cursor || cursors.has(page.next_cursor)) throw new Error('回放分页未完成且无法继续，请重新载入')
    cursors.add(page.next_cursor)
    cursor = page.next_cursor
  }
  throw new Error('回放分页超过读取预算，请缩小窗口')
}
