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
  return apiGet<{ account_config: SignalConfig; quantity_unit: string }>(`/account/${accountId}/signal-config`, signal)
}
