import { apiGet, apiSend } from '@/lib/api/client'
import type { SignalAssetSnapshot, SignalQuoteSource, SignalConfig } from './signalData'

export interface SignalPreview {
  target: { mode: 'quantity'; targets: Record<string, number> }
  quantity_unit: string
}

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

export function getSignalConfig(accountId: number, signal?: AbortSignal) {
  return apiGet<{ account_config: SignalConfig; quantity_unit: string }>(`/account/${accountId}/signal-config`, signal)
}

export function previewSignal(portfolioId: number, config: SignalConfig) {
  return apiSend<SignalPreview>('POST', '/account/signal/preview', { portfolio_id: portfolioId, account_config: config })
}
