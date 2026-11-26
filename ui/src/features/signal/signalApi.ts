import { apiGet, apiSend } from '@/lib/api/client'
import type { AccountFeishuTestResult } from '@/lib/api/accounts'
import type { SignalAssetSnapshot, SignalQuoteSource, SignalConfig } from './signalData'

export interface SignalQuoteTestResult {
  items: Array<{
    symbol: string
    quote: { last_price: number; timestamp: number; bids: [number, number][]; asks: [number, number][] } | null
    error: string | null
  }>
}

export function testSignalQuote(quoteSource: SignalQuoteSource, symbols: string[], signal: AbortSignal) {
  return apiSend<SignalQuoteTestResult>('POST', '/account/signal/quote-test', { quote_source: quoteSource, symbols }, signal)
}

export function testSignalAccount(code: string, name: string, market: string, signal: AbortSignal) {
  return apiSend<SignalAssetSnapshot>('POST', '/account/signal/account-test', { code, name, market }, signal)
}

export function testSignalNotification(mode: 'feishu' | 'function', draft: string, name: string, market: string) {
  return apiSend<AccountFeishuTestResult>('POST', '/account/signal/notification-test', { mode, draft, name, market })
}

export function getSignalConfig(accountId: number, signal?: AbortSignal) {
  return apiGet<{ account_config: SignalConfig }>(`/account/${accountId}/signal-config`, signal)
}

export type SignalRecordStatus = 'SUCCEEDED' | 'FAILED'

export interface SignalRecord {
  id: string
  created_at: string
  scheduled_at: string | null
  trigger_source: 'manual' | 'scheduler'
  status: SignalRecordStatus
  error: string | null
  quantity_unit: string
  targets: Record<string, number>
  notification_status: 'pending' | 'calling' | 'succeeded' | 'failed' | 'unknown'
  notification_error: string | null
}

export interface SignalRecords {
  items: SignalRecord[]
  next_cursor: number | null
}

export function getSignalRecords(accountId: number, options: {
  limit: number
  before?: number | null
  days?: number | null
  status?: SignalRecordStatus | null
  symbol?: string
}, signal: AbortSignal) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(options)) {
    if (value != null && value !== '') query.set(key, String(value))
  }
  return apiGet<SignalRecords>(`/account/${accountId}/signal/records?${query}`, signal)
}
