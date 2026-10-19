import { apiGet, apiSend } from '@/lib/api/client'

export interface TimeCheckSettings {
  version: number
  source: 'aliyun' | 'tencent' | 'tuna' | 'custom'
  custom_host: string
  custom_port: number
  threshold_ms: number
  enabled: boolean
  cron_expr: string
}
export interface ProbeResult {
  status: string
  error?: string
  enabled?: boolean | null
  synchronized?: boolean | null
  evidence?: string
  offset_ms?: number
  delay_ms?: number
  samples?: number
  sample_errors?: string[]
}
export interface TimeCheckSnapshot {
  stream_id?: string
  version?: number
  ready: boolean
  settings: TimeCheckSettings
  status: 'warning' | 'enabled' | 'disabled'
  next_run_at: string | null
  state: {
    running?: boolean
    started_at?: string
    trigger?: string
    alerts?: Record<string, { message: string; checked_at: string; offset_ms?: number; threshold_ms?: number }>
    result?: { finished_at: string; trigger: string; host: string; port: number; settings: TimeCheckSettings; ntp: ProbeResult; system: ProbeResult }
    last_success?: { checked_at: string; host: string; offset_ms: number; delay_ms: number }
  }
}
export interface TimeCheckEnvelope { stream_id: string; version: number; data: TimeCheckSnapshot }
const path = '/system/time-check'
export const getTimeCheck = () => apiGet<TimeCheckSnapshot>(path)
export const saveTimeCheck = (value: TimeCheckSettings) => apiSend<TimeCheckSnapshot>('PUT', `${path}/settings`, value)
export const runTimeCheck = () => apiSend<TimeCheckSnapshot>('POST', `${path}/run`)
export const previewTimeCheck = (cron_expr: string, signal?: AbortSignal) => apiSend<{ timezone: string; times: string[] }>('POST', `${path}/schedule-preview`, { cron_expr }, signal)
