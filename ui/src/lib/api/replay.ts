import { ApiError, apiGet } from '@/lib/api/client'
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

export class ReplayWindowBudgetError extends Error {
  constructor() { super('当前时刻的记录过多，无法在读取预算内展示'); this.name = 'ReplayWindowBudgetError' }
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
  if (row.execution_start_us != null && (!Number.isSafeInteger(row.execution_start_us) || Number(row.execution_start_us) < 0)) throw new Error('执行开始时间无效')
  for (const item of row.recordings) {
    const recording = object(item)
    if (typeof recording.recording_id !== 'string' || !ranges(recording.coverage)) throw new Error('回放目录包含无效覆盖范围')
    if (recording.symbols !== undefined && (!Array.isArray(recording.symbols) || recording.symbols.some(symbol => typeof symbol !== 'string' || !symbol || symbol.length > 256 || [...symbol].some(character => character < ' ')))) throw new Error('回放目录包含无效品种目录')
    if (recording.symbols_complete !== undefined && typeof recording.symbols_complete !== 'boolean') throw new Error('回放品种目录完整性无效')
    if (recording.symbols_complete === true && recording.symbols === undefined) throw new Error('完整品种目录缺少名称列表')
    const context = object(recording.context)
    if (context.schema_version !== 1) throw new Error('该录制版本暂不支持，请升级后查看')
  }
  return value as ReplayManifest
}

function validateReplayMessage(raw: unknown, request: ReplayWindowRequest): ReplayMessage {
  const message = object(raw)
  if (message.schema_version !== 1 || message.recording_id !== request.recording_id || !Number.isSafeInteger(message.capture_seq) || Number(message.capture_seq) <= 0 || !Number.isSafeInteger(message.time_us)) throw new Error('回放消息身份、版本或顺序无效')
  if (Number(message.time_us) < request.start_us || Number(message.time_us) >= request.end_us || typeof message.topic !== 'string') throw new Error('回放消息超出所选窗口')
  object(message.data)
  return raw as ReplayMessage
}

export function validateReplayPage(value: unknown, request: ReplayWindowRequest, windowId?: string): ReplayWindow {
  const row = object(value)
  if (row.recording_id !== request.recording_id || typeof row.window_id !== 'string' || (windowId && row.window_id !== windowId)) throw new Error('回放窗口已变化，请重新载入')
  if (!Array.isArray(row.messages) || row.messages.length > 2000 || !ranges(row.coverage) || !Array.isArray(row.gaps) || typeof row.complete !== 'boolean') throw new Error('回放分页结构或数量超出约定')
  if (row.next_cursor !== null && typeof row.next_cursor !== 'string') throw new Error('回放游标无效')
  if (row.state_fragment !== undefined) {
    if (typeof row.state_fragment !== 'string' || !row.state_fragment || typeof row.state_fragment_end !== 'boolean'
      || row.state_before_start != null || row.complete || row.messages.length !== 0) throw new Error('回放基线分页结构无效')
  } else if (row.state_fragment_end !== undefined) throw new Error('回放基线分页结构无效')
  if (row.message_fragment !== undefined) {
    if (typeof row.message_fragment !== 'string' || !row.message_fragment || typeof row.message_fragment_end !== 'boolean'
      || row.state_fragment !== undefined || row.complete || row.messages.length !== 0) throw new Error('回放事件分页结构无效')
  } else if (row.message_fragment_end !== undefined) throw new Error('回放事件分页结构无效')
  if (object(row.context).schema_version !== 1) throw new Error('该录制版本暂不支持，请升级后查看')
  if (new TextEncoder().encode(JSON.stringify(value)).byteLength > MAX_PAGE_BYTES) throw new Error('回放单页超过 4 MiB，请缩小窗口')
  let previous = -1
  let previousTime = Number.NEGATIVE_INFINITY
  for (const raw of row.messages) {
    const message = validateReplayMessage(raw, request)
    if (Number(message.capture_seq) <= previous) throw new Error('回放消息身份、版本或顺序无效')
    if (Number(message.time_us) < previousTime) throw new Error('录制段内观察时间回退')
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

/** 固定窗口逐页读取；大基线通过有序分片恢复。 */
export async function readReplayWindow(executionId: string, request: ReplayWindowRequest, signal?: AbortSignal): Promise<ReplayWindow> {
  const messages: ReplayMessage[] = []
  const cursors = new Set<string>()
  const fragments: string[] = []
  const messageFragments: string[] = []
  let first: ReplayWindow | null = null
  let baseline: ReplayWindow['state_before_start'] = null
  let fragmentsComplete = false
  let cursor: string | undefined
  for (;;) {
    signal?.throwIfAborted()
    let raw: unknown
    try { raw = await apiGet<unknown>(`/account/executions/${encodeURIComponent(executionId)}/replay/window?${replayWindowQuery(request, cursor)}`, signal) }
    catch (error) { if (error instanceof ApiError && error.code === 'REPLAY_BUDGET') throw new ReplayWindowBudgetError(); throw error }
    const page = validateReplayPage(raw, request, first?.window_id)
    if (!first) {
      first = page
      baseline = page.state_before_start
    }
    if (page.state_fragment !== undefined) {
      if (baseline !== null || fragmentsComplete) throw new Error('回放基线分页顺序无效')
      fragments.push(page.state_fragment)
      if (page.state_fragment_end) {
        try { baseline = object(JSON.parse(fragments.join(''))) }
        catch { throw new Error('回放基线分页内容无效') }
        fragments.length = 0
        fragmentsComplete = true
      }
    } else if (baseline === null) throw new Error('回放窗口缺少恢复基线，无法安全重建')
    let currentMessages = page.messages
    if (page.message_fragment !== undefined) {
      messageFragments.push(page.message_fragment)
      if (page.message_fragment_end) {
        let message: unknown
        try { message = JSON.parse(messageFragments.join('')) }
        catch { throw new Error('回放事件分页内容无效') }
        currentMessages = [validateReplayMessage(message, request)]
        messageFragments.length = 0
      }
    } else if (messageFragments.length) throw new Error('回放事件分页顺序无效')
    const last = messages.at(-1)
    if (last && currentMessages[0] && currentMessages[0].capture_seq <= last.capture_seq) throw new Error('回放分页重复或倒序，请重新载入')
    if (last && currentMessages[0] && currentMessages[0].time_us < last.time_us) throw new Error('录制段内观察时间回退')
    messages.push(...currentMessages)
    if (page.complete) {
      if (baseline === null) throw new Error('回放窗口缺少恢复基线，无法安全重建')
      const result = { ...first, state_before_start: baseline, messages, next_cursor: null, complete: true }
      delete result.state_fragment
      delete result.state_fragment_end
      delete result.message_fragment
      delete result.message_fragment_end
      return result
    }
    if (!page.next_cursor || cursors.has(page.next_cursor)) throw new Error('回放分页未完成且无法继续，请重新载入')
    cursors.add(page.next_cursor)
    cursor = page.next_cursor
  }
}
