/** 执行触发 / 状态 / 审计相关接口。 */
import { apiGet, apiSend } from '@/lib/api/client'
import type {
  ExecutionArtifactList,
  ExecutionEventList,
  ExecutionStatus,
  ExecutionTerminate,
  ExecutionTrigger,
} from '@/types/api'

/** 立即执行一次（异步入队），返回可轮询的 execution_id。触发真实下单，用 POST。 */
export function triggerExecute(accountId: number): Promise<ExecutionTrigger> {
  return apiSend<ExecutionTrigger>('POST', `/account/execute/${accountId}`)
}

/** 一键清仓（异步入队）。触发真实清仓下单，用 POST。 */
export function triggerEmptyPositions(accountId: number): Promise<ExecutionTrigger> {
  return apiSend<ExecutionTrigger>('POST', `/account/empty_positions/${accountId}`)
}

export type ExecutionTerminateMode = 'graceful' | 'cancel_pending'

/** 终止账户当前执行。 */
export function terminateExecution(
  accountId: number,
  body: { reason?: string; mode?: ExecutionTerminateMode } = {},
): Promise<ExecutionTerminate> {
  return apiSend<ExecutionTerminate>('POST', `/account/${accountId}/terminate`, body)
}

/** 轮询执行状态。 */
export function getExecutionStatus(
  executionId: string,
  signal?: AbortSignal,
): Promise<ExecutionStatus> {
  return apiGet<ExecutionStatus>(`/account/executions/${executionId}`, signal)
}

/** 执行事件流。 */
export function getExecutionEvents(
  executionId: string,
  signal?: AbortSignal,
  params: { skip?: number; limit?: number } = {},
): Promise<ExecutionEventList> {
  return apiGet<ExecutionEventList>(`/account/executions/${executionId}/events?${new URLSearchParams(Object.entries(params).map(([key, value]) => [key, String(value)]))}`, signal)
}

/** 执行附件（standard_input / target_snapshot …）。 */
export function getExecutionArtifacts(
  executionId: string,
  signal?: AbortSignal,
  params: { skip?: number; limit?: number } = {},
): Promise<ExecutionArtifactList> {
  return apiGet<ExecutionArtifactList>(`/account/executions/${executionId}/artifacts?${new URLSearchParams(Object.entries(params).map(([key, value]) => [key, String(value)]))}`, signal)
}

/** 执行事实临时组装的详情，尚无结果时返回 null。 */
export function getExecutionDetail(executionId: string, accountId: number, signal?: AbortSignal): Promise<Record<string, unknown> | null> {
  return apiGet(`/account/executions/${executionId}/detail?account_id=${accountId}`, signal)
}
