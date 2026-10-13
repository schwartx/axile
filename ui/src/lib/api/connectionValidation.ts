import { apiGet, apiSend } from '@/lib/api/client'

export type ValidationStepStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped'
export interface ConnectionValidationError {
  reason_code: string
  message: string
  error_id?: number | null
  details?: string | null
}
export interface ConnectionValidationJob {
  validation_id: string
  channel: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  steps: Array<{ id: string; label: string; status: ValidationStepStatus; error?: ConnectionValidationError | null }>
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
