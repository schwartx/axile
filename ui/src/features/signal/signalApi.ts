import { apiGet, apiSend } from '@/lib/api/client'
import type { SignalConfig } from './signalData'

export interface SignalPreview {
  target: { mode: 'quantity'; targets: Record<string, number> }
  quantity_unit: string
}

export function getSignalConfig(accountId: number, signal?: AbortSignal) {
  return apiGet<{ account_config: SignalConfig; quantity_unit: string }>(`/account/${accountId}/signal-config`, signal)
}

export function previewSignal(portfolioId: number, config: SignalConfig) {
  return apiSend<SignalPreview>('POST', '/account/signal/preview', { portfolio_id: portfolioId, account_config: config })
}
