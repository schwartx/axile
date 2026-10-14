import { errorValueText, shortErrorReason, type ErrorEvidence, type ErrorInfo } from '@/lib/errorInfo'
import type { ExecuteRecord, ExecutionEvent, ExecutionArtifact, ExecutionStatus } from '@/types/api'

export interface EvidencePage<T> { data: T[]; count: number | null; complete: boolean; error: Error | null }

/** 独立读取每个来源的全部页；失败时保留此前已读内容与真实范围。 */
export async function readEvidencePages<T extends { id: number | null }>(
  fetchPage: (skip: number, limit: number) => Promise<{ data: T[]; count: number }>, signal: AbortSignal,
): Promise<EvidencePage<T>> {
  const data: T[] = []
  const ids = new Set<number>()
  let count = 0
  let captured = false
  try {
    for (;;) {
      signal.throwIfAborted()
      const page = await fetchPage(data.length, 500)
      signal.throwIfAborted()
      if (!Number.isInteger(page.count) || page.count < 0 || !Array.isArray(page.data)) throw new Error('证据分页响应无效')
      if (!captured) { count = page.count; captured = true }
      if (page.count < count) throw new Error('证据在读取期间减少，请重新读取')
      if (!page.data.length && data.length < count) throw new Error('证据分页不完整，请重新读取')
      for (const item of page.data.slice(0, count - data.length)) {
        if (item.id != null && ids.has(item.id)) throw new Error('证据分页重叠，请重新读取')
        if (item.id != null) ids.add(item.id)
        data.push(item)
      }
      if (data.length >= count) return { data, count, complete: true, error: null }
    }
  } catch (cause) {
    signal.throwIfAborted()
    return { data, count: captured ? count : null, complete: false, error: cause instanceof Error ? cause : new Error(String(cause)) }
  }
}

const dict = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const knownFields = ['error', 'technical_detail', 'traceback', 'formatted_traceback', 'outcome_reason', 'reason_code', 'error_id', 'diagnostics']
function errorFields(value: unknown, label: string, nested = false): ErrorEvidence[] {
  const source = dict(value)
  const fields = nested ? [...knownFields, 'message', 'details'] : knownFields
  const own = fields.flatMap(key => source[key] != null && source[key] !== '' ? [{ label: `${label} · ${key}`, value: errorValueText(source[key]) }] : [])
  if (!nested) {
    for (const key of ['error', 'technical_detail']) {
      if (typeof source[key] === 'object' && source[key] != null) own.push(...errorFields(source[key], `${label} · ${key}`, true))
    }
  }
  const worker = dict(dict(source.extra).worker_error)
  if (Object.keys(worker).length) { own.push({ label: `${label} · worker`, value: errorValueText(worker) }); own.push(...errorFields(worker, `${label} · worker`, true)) }
  const debugError = dict(source.debug).error
  if (debugError != null) { own.push({ label: `${label} · 已有错误`, value: errorValueText(debugError) }); if (typeof debugError === 'object') own.push(...errorFields(debugError, `${label} · 已有错误`, true)) }
  for (const [symbol, result] of Object.entries(dict(source.symbol_results))) {
    own.push(...errorFields(result, `${label} · ${symbol}`, true))
  }
  return own
}

export const catalogLabels: Record<string, string> = { instruments_waiting: '等待合约资料', instruments_ready: '合约资料就绪', instruments_failed: '合约资料失败' }
export function catalogEvidence(event: ExecutionEvent): ErrorEvidence | null {
  const label = catalogLabels[event.event_type]
  if (!label) return null
  const d = event.details
  return { label: `${label} · 事件 ${event.id ?? event.seq}`, value: [
    d.message == null ? null : errorValueText(d.message),
    `原因代码：${event.reason_code}`,
    d.symbols == null ? null : `品种：${errorValueText(d.symbols)}`,
    d.elapsed_seconds == null ? null : `耗时：${d.elapsed_seconds} 秒`,
  ].filter(item => item != null).join('\n') }
}

/** 技术载荷只在专用适配器解析；既有原文逐字保留。 */
export function buildExecutionErrorInfo(input: {
  accountId: number; executionId?: string | null; recordId?: number | null; record?: ExecuteRecord | null
  status?: ExecutionStatus | null; events?: ExecutionEvent[]; artifacts?: ExecutionArtifact[]; scope?: ErrorEvidence[]
}): ErrorInfo {
  const evidence: ErrorEvidence[] = [{ label: '账户', value: String(input.accountId) }]
  if (input.executionId) evidence.push({ label: 'execution_id', value: input.executionId })
  if (input.record?.id ?? input.recordId) evidence.push({ label: '记录 ID', value: String(input.record?.id ?? input.recordId) })
  if (input.status) evidence.push({ label: '最后已知状态', value: input.status.status })
  evidence.push(...(input.scope ?? []))
  const details: ErrorEvidence[] = []
  if (input.record) details.push(...errorFields(input.record.raw_result, `记录 ${input.record.id}`))
  if (input.status?.error) details.push({ label: '执行状态 · 原因', value: input.status.error })
  for (const event of input.events ?? []) {
    const catalog = catalogEvidence(event)
    if (catalog) details.push(catalog)
    details.push(...errorFields(event.details, `事件 ${event.id ?? event.seq} · ${event.event_type}${event.symbol ? ` · ${event.symbol}` : ''}`))
  }
  for (const artifact of input.artifacts ?? []) details.push(...errorFields(artifact.content, `附件 ${artifact.id} · ${artifact.artifact_type}`))
  if (!details.length) details.push({ label: '已保存内容', value: '现有记录、事件和附件没有保存错误原文或 traceback。历史未保存的内容无法补回。' })
  const text = [...evidence.map(({ label, value }) => `${label}：${value}`), ...details.map(({ label, value }) => `${label}\n${value}`)].join('\n\n')
  return { evidence, details, text, message: shortErrorReason(details[0]?.value) }
}
