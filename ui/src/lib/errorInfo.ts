import { ApiError } from '@/lib/api/client'

export interface ErrorEvidence {
  label: string
  value: string
}

export interface ErrorInfo {
  message: string
  evidence: ErrorEvidence[]
  details: ErrorEvidence[]
  text: string
}

/** JSON 不支持的值保留字符串形式；错误内容不脱敏、不截断。 */
export function errorValueText(value: unknown): string {
  if (typeof value === 'string') return value
  try { return JSON.stringify(value, (_key, item) => ['bigint', 'undefined', 'function', 'symbol'].includes(typeof item) || (typeof item === 'number' && !Number.isFinite(item)) ? String(item) : item, 2) ?? String(value) }
  catch { return String(value) }
}

/** 判断浏览器 fetch 是否在收到 HTTP 响应前失败。 */
export function isConnectionError(error: unknown): boolean {
  if (!(error instanceof TypeError)) return false
  return ['Failed to fetch', 'Load failed', 'NetworkError when attempting to fetch resource.'].includes(error.message)
}

export function shortErrorReason(error: unknown, maxLength = 160): string {
  const value = error && typeof error === 'object' && 'message' in error ? error.message : error
  const raw = error instanceof Error ? error.message : typeof value === 'string' ? value : errorValueText(value ?? '')
  const normalized = raw.replace(/\s+/g, ' ').trim()
  const message = isConnectionError(error)
    ? '无法连接 axile 服务'
    : normalized || '未知错误'
  return message.length <= maxLength ? message : `${message.slice(0, maxLength - 1)}…`
}

/** 在用户打开时调用，正文与复制使用同一份字符串快照。 */
export function errorInfo(error: unknown, context: ErrorEvidence[] = []): ErrorInfo {
  const evidence: ErrorEvidence[] = [...context]
  const details: ErrorEvidence[] = [{ label: '错误原文', value: error instanceof Error ? error.message : errorValueText(error) }]
  if (error instanceof ApiError) {
    evidence.push({ label: 'HTTP', value: String(error.status) })
    if (error.code) evidence.push({ label: '错误码', value: error.code })
    if (error.requestId) evidence.push({ label: '请求标识', value: error.requestId })
    const payload = error.body && typeof error.body === 'object' ? error.body as Record<string, unknown> : {}
    const nested = payload.detail && typeof payload.detail === 'object' ? payload.detail as Record<string, unknown> : {}
    for (const [prefix, source] of [['响应', payload], ['detail', nested]] as const) {
      for (const key of ['technical_detail', 'details', 'traceback', 'formatted_traceback', 'validation_errors']) {
        if (source[key] != null) details.push({ label: `${prefix} · ${key}`, value: errorValueText(source[key]) })
      }
    }
    if (error.body != null) details.push({ label: '响应对象', value: errorValueText(error.body) })
    if (error.rawText != null) details.push({ label: '原始响应', value: error.rawText })
  }
  if (error instanceof Error && error.stack) details.push({ label: '已有 stack', value: error.stack })
  return errorInfoFromEntries(shortErrorReason(error), evidence, details)
}

export function errorInfoFromEntries(message: string, evidence: ErrorEvidence[], details: ErrorEvidence[]): ErrorInfo {
  const text = [...evidence.map(({ label, value }) => `${label}：${value}`),
    ...details.map(({ label, value }) => `${label}\n${value}`)].join('\n\n')
  return { message, evidence, details, text }
}
