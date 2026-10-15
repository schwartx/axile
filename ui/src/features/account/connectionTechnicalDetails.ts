import type { AccountConnectionDetail } from '@/types/api'
import { errorValueText } from '@/lib/errorInfo'

export interface TechnicalEntry { key: string; label: string; value: string }
export interface TechnicalSection extends TechnicalEntry { kind: 'text' | 'stack' | 'json' }
export interface ConnectionTechnicalInfo {
  message: string
  fields: TechnicalEntry[]
  sections: TechnicalSection[]
  stackNotice: string
}

const labels: Record<string, string> = {
  message: '错误信息', technical_detail: '技术说明', exception_type: '异常类型', error_scope: '错误范围',
  reason_code: '原因码', error_id: '渠道错误码', status: '错误码', status_code: '状态码', return_code: '返回码',
  broker_message: '柜台原文', traceback: '原始异常栈', formatted_traceback: '原始异常栈',
  propagation_traceback: '异常传播栈', reporting_stack: '上报现场栈',
}
const stackKeys = ['traceback', 'formatted_traceback', 'propagation_traceback', 'reporting_stack']

function stackSections(error: Record<string, unknown>, message: string) {
  const sections: TechnicalSection[] = []
  const seen = new Set([message.trim()])
  let hasExceptionStack = false
  for (const key of stackKeys) {
    const value = error[key]
    if (typeof value !== 'string' || !value.trim()) continue
    const isStack = /^\s*(?:\|\s*)*File ".+", line \d+/m.test(value)
    if (key !== 'reporting_stack' && isStack) hasExceptionStack = true
    if (seen.has(value.trim())) continue
    seen.add(value.trim())
    // format_exception 对尚未 raise 的异常只生成这一行；类型和技术说明已分别展示。
    const summary = `${error.exception_type}: ${error.technical_detail}`
    if (!isStack && (value.trim() === summary || value.trim().endsWith(`.${summary}`))) continue
    sections.push({ key, label: isStack ? labels[key]! : '异常摘要', value, kind: isStack ? 'stack' : 'text' })
  }
  return { sections, seen, hasExceptionStack }
}

function evidenceEntries(error: Record<string, unknown>, seen: Set<string>) {
  const fields: TechnicalEntry[] = []
  const sections: TechnicalSection[] = []
  for (const [key, value] of Object.entries(error)) {
    if (value == null || stackKeys.includes(key) || key === 'traceback_unavailable_reason') continue
    const raw = errorValueText(value)
    if (!raw.trim()) continue
    const entry = { key, label: labels[key] ?? key, value: key === 'error_scope' && raw === 'account_connection' ? '账户连接' : raw }
    if (['message', 'technical_detail', 'broker_message'].includes(key)) {
      if (!seen.has(raw.trim())) sections.push({ ...entry, kind: 'text' })
      seen.add(raw.trim())
    } else if (typeof value === 'object') sections.push({ ...entry, kind: 'json' })
    else fields.push(entry)
  }
  return { fields, sections }
}

/** 只对阅读内容去重；完整复制仍使用接口原始快照。旧记录中的异常摘要不冒充调用栈。 */
export function connectionTechnicalDetails(detail: AccountConnectionDetail): ConnectionTechnicalInfo | null {
  if (detail.status !== 'error') return null
  const error = detail.error ?? {}
  const message = detail.message || (typeof error.message === 'string' ? error.message : '')
  const stacks = stackSections(error, message)
  const content = evidenceEntries(error, stacks.seen)
  const sections = [...stacks.sections, ...content.sections]
  // 先读技术说明，再看独立的代码区域；不同栈来源保留自己的标题。
  sections.sort((a, b) => Number(a.kind === 'stack') - Number(b.kind === 'stack'))
  const stackNotice = stacks.hasExceptionStack ? '' : typeof error.traceback_unavailable_reason === 'string'
    ? error.traceback_unavailable_reason : '这条记录未包含异常调用帧；已有的异常摘要仍保留在详情中。'
  return { message, fields: content.fields, sections, stackNotice }
}
