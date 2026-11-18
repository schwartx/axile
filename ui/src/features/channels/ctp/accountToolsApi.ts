import { apiGet } from '@/lib/api/client'

export interface MarginRates {
  instrument_id: string
  is_relative: boolean
  long_ratio: number
  long_per_lot: number
  short_ratio: number
  short_per_lot: number
}

export interface CommissionRates {
  instrument_id: string
  open_ratio: number
  open_per_lot: number
  close_ratio: number
  close_per_lot: number
  close_today_ratio: number
  close_today_per_lot: number
}

export interface SettlementDocument {
  trading_day: string
  settlement_id: number
  currency: string
  content: string
}

export function getMarginRates(id: number, symbol: string, signal: AbortSignal) {
  return apiGet<MarginRates>(`/account/${id}/margin-rates?${new URLSearchParams({ symbol })}`, signal)
}

export function getCommissionRates(id: number, symbol: string, signal: AbortSignal) {
  return apiGet<CommissionRates>(`/account/${id}/commission-rates?${new URLSearchParams({ symbol })}`, signal)
}

export function getSettlementStatement(id: number, day: string, signal: AbortSignal) {
  const query = day ? `?${new URLSearchParams({ trading_day: day })}` : ''
  return apiGet<{ documents: SettlementDocument[] }>(`/account/${id}/settlement-statement${query}`, signal)
}
