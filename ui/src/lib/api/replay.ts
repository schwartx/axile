import { apiGet } from '@/lib/api/client'

export interface ReplayStatus {
  desired: boolean
  state: 'disabled' | 'waiting_connection' | 'starting' | 'recording' | 'closing' | 'error' | 'unsupported'
  config_version: number
  effective_version?: number | null
  recording_id?: string | null
  reason?: string | null
  error?: string | null
}

export function getReplayStatus(accountId: number, signal?: AbortSignal): Promise<ReplayStatus> {
  return apiGet(`/account/${accountId}/replay/status`, signal)
}
