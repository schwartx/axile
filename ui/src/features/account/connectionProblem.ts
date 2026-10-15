import type { AccountConnection, AccountConnectionDetail, ConnectionPresentation } from '@/types/api'
import { errorValueText } from '@/lib/errorInfo'
import { connectionTechnicalDetails } from '@/features/account/connectionTechnicalDetails'

const text = (value: unknown): string => typeof value === 'string' ? value.trim() : ''
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : []

export function connectionLabel(connection?: AccountConnection): string {
  const labels = { uninitialized: '未建立会话', unknown: '状态待确认', ready: '会话就绪', unavailable: '暂不可用', connecting: '正在准备', recovering: '正在恢复', disconnected: '已断开' }
  if (connection?.status === 'unavailable') return text(connection.label) || labels.unavailable
  return connection ? labels[connection.status] ?? labels.unknown : labels.uninitialized
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
  const failed = detail.status === 'unavailable'
  const presentation = failed ? connectionPresentation(detail.presentation) : {}
  const label = connectionLabel(detail)
  const title = failed ? presentation.title || text(detail.label) || '账户会话暂不可用' : label
  const summaries = {
    ready: '当前执行器正在驻留，账户会话已准备并完成核对。最近一次账户访问时间见下方；单次请求成功不保证后续请求一定成功。',
    disconnected: detail.manually_disconnected ? '账户会话已主动断开。查询和计划执行不会自动登录，需要显式重新连接。' : '账户会话已结束，可重新建立连接。',
    connecting: '账户正在准备会话，完成后会核对资产和未完成委托。',
    recovering: '账户正在恢复会话，完成核对后才会重新标记为就绪。',
    unknown: '当前会话状态待确认；上次成功的访问记录不能证明当前通道可用。',
    uninitialized: '当前服务尚未建立该账户会话。查询或显式连接可以按需建立会话。',
    unavailable: '账户会话暂不可用，具体原因见下方。',
  }
  const summary = failed ? presentation.summary || detail.message || summaries.unavailable : summaries[detail.status] ?? summaries.unknown
  const at = (value?: string | null) => value?.replace('T', ' ') ?? '尚无记录'
  const facts = [
    { label: '执行器', value: detail.worker_alive ? '正在驻留' : '未驻留' },
    { label: '状态更新', value: at(detail.observed_at) },
    { label: '最近访问成功', value: at(detail.last_success_at) },
    { label: '最近连接失败', value: at(detail.last_failure_at) },
  ]
  if (detail.retry_after_seconds) facts.push({ label: '恢复冷却', value: `${detail.retry_after_seconds} 秒后可尝试恢复` })
  // 正常/未知响应即使包含旧展示字段，也不能继续展示旧故障建议或原始错误。
  const evidence = failed ? { message: detail.message, error: detail.error } : null
  const technical = evidence ? [evidence.message, ...Object.entries(evidence.error ?? {}).map(([key, value]) => `${key}\n${errorValueText(value)}`)].filter(Boolean).join('\n\n') : ''
  const historical = !failed && detail.last_failure ? detail.last_failure : null
  const history = historical ? {
    ...historical,
    technicalInfo: connectionTechnicalDetails({ ...detail, status: 'unavailable', message: historical.message ?? null, error: historical.error ?? null }),
  } : null
  const sections = presentation.sections ?? []
  const actions = presentation.actions ?? []
  const copy = [title, summary, `账户：${accountId}`, `当前会话：${label}`, ...facts.map(field => `${field.label}：${field.value}`), ...sections.flatMap(section => [section.title, section.text,
    ...(section.fields ?? []).map(field => `${field.label}：${field.value}`), ...(section.steps ?? []).map((step, i) => `${i + 1}. ${step}`)]),
  ...actions.map(action => action.label), technical, history ? `最近连接失败（历史）\n${at(history.observed_at)}\n${history.message ?? ""}\n${errorValueText(history.error ?? {})}\n${errorValueText(history.presentation ?? {})}` : ""].filter(Boolean).join('\n\n')
  return { title, summary, facts, sections, actions, technical, history, technicalInfo: connectionTechnicalDetails(detail), copy }
}
