import type { AccountConnection, AccountConnectionDetail, ConnectionPresentation } from '@/types/api'
import { errorValueText } from '@/lib/errorInfo'
import { connectionTechnicalDetails } from '@/features/account/connectionTechnicalDetails'

const text = (value: unknown): string => typeof value === 'string' ? value.trim() : ''
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : []

export function connectionLabel(connection?: AccountConnection): string {
  if (connection?.status === 'ok') return '连接正常'
  if (connection?.status !== 'error') return '连接未知'
  return text(connection.label) || '连接异常'
}

/** 逐块保留可用内容，未知意图与无效字段不影响其他区块。 */
export function connectionPresentation(value: unknown): ConnectionPresentation {
  const input = record(value)
  const sections = array(input.sections).flatMap(value => {
    const section = record(value)
    const fields = array(section.fields).flatMap(value => {
      const field = record(value)
      return text(field.label) && text(field.value) ? [{ label: text(field.label), value: text(field.value) }] : []
    })
    const steps = array(section.steps).map(text).filter(Boolean)
    const content = text(section.text)
    return content || fields.length || steps.length ? [{ title: text(section.title), text: content, fields, steps }] : []
  })
  const actions = array(input.actions).flatMap(value => {
    const action = record(value)
    return action.intent === 'open_connection_settings' && text(action.label) ? [{ label: text(action.label), intent: 'open_connection_settings' }] : []
  })
  return { title: text(input.title), summary: text(input.summary), sections, actions }
}

export function connectionProblem(accountId: number, detail: AccountConnectionDetail) {
  const presentation = detail.status === 'error' ? connectionPresentation(detail.presentation) : {}
  const label = connectionLabel(detail)
  const title = detail.status === 'error' ? presentation.title || text(detail.label) || '账户连接异常' : label
  const summary = detail.status === 'error'
    ? presentation.summary || detail.message || '最近一次连接或账户访问报告异常，具体原因尚未确认。'
    : detail.status === 'ok' ? '最近一次连接或账户访问已确认正常。' : '当前配置尚无已确认的连接结果。'
  const facts = [{ label: '账户', value: String(accountId) }, { label: '连接状态', value: label }, { label: '最近确认', value: detail.observed_at?.replace('T', ' ') ?? '尚未确认' }]
  // 正常/未知响应即使包含旧展示字段，也不能继续展示旧故障建议或原始错误。
  const evidence = detail.status === 'error' ? { message: detail.message, error: detail.error } : null
  const technical = evidence ? [evidence.message, ...Object.entries(evidence.error ?? {}).map(([key, value]) => `${key}\n${errorValueText(value)}`)].filter(Boolean).join('\n\n') : ''
  const sections = presentation.sections ?? []
  const actions = presentation.actions ?? []
  const copy = [title, summary, ...facts.map(field => `${field.label}：${field.value}`), ...sections.flatMap(section => [section.title, section.text,
    ...(section.fields ?? []).map(field => `${field.label}：${field.value}`), ...(section.steps ?? []).map((step, i) => `${i + 1}. ${step}`)]),
  ...actions.map(action => action.label), technical].filter(Boolean).join('\n\n')
  return { title, summary, facts, sections, actions, technical, technicalInfo: connectionTechnicalDetails(detail), copy }
}
