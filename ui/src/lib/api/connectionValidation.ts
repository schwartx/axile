import { apiGet, apiSend } from '@/lib/api/client'

export type ValidationStepStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped'
export interface ConnectionValidationError {
  reason_code: string
  message: string
  error_id?: number | null
  details?: string | null
}
export type ValidationCell = string | number | boolean | null

export interface ValidationKeyValueDetails {
  title: string
  rows: Array<{ key: string; value: ValidationCell }>
}
export interface ValidationTableColumn {
  key: string
  label: string
  type: 'text' | 'number' | 'date' | 'time'
  filterable?: boolean
}
export interface ValidationTableSummary {
  kind: 'table'
  title: string
  summary: string
  description?: string | null
  columns: ValidationTableColumn[]
  row_count: number
}
export interface ValidationTable extends ValidationTableSummary {
  rows: ValidationCell[][]
}
export type ValidationStepDetails = ValidationKeyValueDetails | ValidationTableSummary
export function isValidationTable(details: ValidationStepDetails): details is ValidationTableSummary {
  return 'kind' in details && details.kind === 'table'
}

/** 表格快照只在查看时读取一次，独立于阶段状态的轮询。 */
export const getValidationTable = (id: string, stepId: string, signal?: AbortSignal) => apiGet<ValidationTable>(
  `/channel-validations/${encodeURIComponent(id)}/steps/${encodeURIComponent(stepId)}/table`, signal,
)

export interface ConnectionValidationJob {
  validation_id: string
  channel: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  steps: Array<{ id: string; label: string; status: ValidationStepStatus; error?: ConnectionValidationError | null; details?: ValidationStepDetails | null }>
  created_at: string
  finished_at: string | null
  error: ConnectionValidationError | null
}

export const connectionValidationApi = {
  create: (channel: string, account_config: Record<string, unknown>) =>
    apiSend<ConnectionValidationJob>('POST', '/channel-validations', { channel, account_config }),
  get: (id: string, signal?: AbortSignal) =>
    apiGet<ConnectionValidationJob>(`/channel-validations/${encodeURIComponent(id)}`, signal),
  cancel: (id: string) =>
    apiSend<ConnectionValidationJob>('DELETE', `/channel-validations/${encodeURIComponent(id)}`),
}

export function validationRunning(job: ConnectionValidationJob): boolean {
  return job.status === 'queued' || job.status === 'running'
}

/** 已有账户由服务端合并保存凭据与当前草稿，查询和取消复用通用任务接口。 */
export function connectionValidationApiForAccount(accountId: number): typeof connectionValidationApi {
  return {
    ...connectionValidationApi,
    create: (_channel, account_config) => apiSend<ConnectionValidationJob>(
      'POST', `/account/${accountId}/connection-validation`, { account_config },
    ),
  }
}
