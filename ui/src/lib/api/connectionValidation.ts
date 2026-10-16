import { apiGet, apiSend } from '@/lib/api/client'

import type { ConnectionRun } from '@/lib/api/connectionProgress'

export interface ConnectionValidationJob extends ConnectionRun {
  validation_id: string
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
