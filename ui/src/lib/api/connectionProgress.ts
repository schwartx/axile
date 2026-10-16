import { apiGet } from '@/lib/api/client'

export type ConnectionStepStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped'
export interface ConnectionStepError {
  reason_code: string
  message: string
  error_id?: number | null
  details?: string | null
  traceback?: string | null
}
export type ConnectionCell = string | number | boolean | null

export interface ConnectionKeyValueDetails {
  title: string
  rows: Array<{ key: string; value: ConnectionCell }>
}
export interface ConnectionTableColumn {
  key: string
  label: string
  type: 'text' | 'number' | 'date' | 'time'
  filterable?: boolean
}
export interface ConnectionTableSummary {
  kind: 'table'
  title: string
  summary: string
  description?: string | null
  columns: ConnectionTableColumn[]
  row_count: number
}
export interface ConnectionTable extends ConnectionTableSummary {
  rows: ConnectionCell[][]
}
export type ConnectionStepDetails = ConnectionKeyValueDetails | ConnectionTableSummary
export function isConnectionTable(details: ConnectionStepDetails): details is ConnectionTableSummary {
  return 'kind' in details && details.kind === 'table'
}


export interface ConnectionStep {
  id: string
  label: string
  status: ConnectionStepStatus
  started_at?: string | null
  finished_at?: string | null
  error?: ConnectionStepError | null
  details?: ConnectionStepDetails | null
}
export interface ConnectionRun {
  run_id: string
  channel: string
  purpose: 'connect' | 'recovery' | 'validation'
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  steps: ConnectionStep[]
  created_at: string
  finished_at: string | null
  error: ConnectionStepError | null
}
export type ConnectionProgressSource = { kind: 'validation'; validationId: string }
  | { kind: 'account'; accountId: number; runId: string }

export function connectionSourceKey(source: ConnectionProgressSource): string {
  return source.kind === 'validation' ? `validation/${source.validationId}` : `account/${source.accountId}/${source.runId}`
}
export function getConnectionTable(source: ConnectionProgressSource, stepId: string, signal?: AbortSignal): Promise<ConnectionTable> {
  const prefix = source.kind === 'validation' ? `/channel-validations/${encodeURIComponent(source.validationId)}`
    : `/account/${source.accountId}/connection/progress/${encodeURIComponent(source.runId)}`
  return apiGet<ConnectionTable>(`${prefix}/steps/${encodeURIComponent(stepId)}/table`, signal)
}

/** 数值表示完成步骤，不估算耗时；会话核对完成前不显示 100%。 */
export function connectionCompletion(progress: Pick<ConnectionRun, 'steps' | 'status'>) {
  const total = progress.steps.length
  const passed = progress.steps.filter(step => step.status === 'passed').length
  const completed = progress.status === 'succeeded' ? total : Math.min(passed, Math.max(0, total - 1))
  return { completed, total, percent: total ? completed / total * 100 : 0 }
}
