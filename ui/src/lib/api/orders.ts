import { apiGet, apiSend } from '@/lib/api/client'

export interface CurrentOrder {
  avg_price?: number | null
  avg_price_source?: 'native' | 'trades' | null
  attributes?: Record<string, string | number | boolean>
  order_id: string
  symbol: string
  direction: string
  offset: string | null
  order_type: string
  price: number
  volume: number
  filled_volume: number
  remaining_volume: number
  status: string
  create_time: string | null
  execution_id: string | null
  native_identity: Record<string, string>
  uncertain: boolean
  update_time?: string | null
  reason?: string | null
}

export interface CancelReceipt {
  request_id: string
  order_id: string
  symbol: string
  state: 'submitting' | 'submitted' | 'unknown' | 'failed' | 'terminal'
  message: string
  requested_at: string
  order: CurrentOrder | null
}

export interface CurrentOrdersSnapshot {
  data: CurrentOrder[]
  count: number | null
  complete: boolean
  sampled_at: string
  instance_id?: string | null
  version?: number
  error?: string | null
  cancellations: CancelReceipt[]
}

export function getCurrentOrders(accountId: number, signal?: AbortSignal, refresh = false): Promise<CurrentOrdersSnapshot> {
  return apiGet(`/account/${accountId}/orders${refresh ? '?refresh=true' : ''}`, signal)
}

export function cancelCurrentOrder(accountId: number, order: CurrentOrder, requestId: string): Promise<CancelReceipt> {
  return apiSend('POST', `/account/${accountId}/orders/cancel`, {
    request_id: requestId, order_id: order.order_id, symbol: order.symbol,
  })
}

export interface AccountTrade {
  trade_id: string
  order_id: string
  symbol: string
  direction: string
  offset: string | null
  time: string
  price: number
  volume: number
  value: number
  trading_day?: string | null
  hedge_flag?: string | null
  exchange_trade_id?: string | null
  amount?: number | null
  commission?: number | null
  commission_asset?: string | null
  cost?: number | null
  realized_pnl?: number | null
  is_maker?: boolean | null
  position_side?: string | null
}

export interface OperationFailure {
  event_id: string
  operation: string
  outcome?: string
  symbol: string
  order_id: string | null
  time: string
  message: string
}

export interface AccountActivity {
  channel?: string
  orders: CurrentOrder[]
  ended_orders?: CurrentOrder[]
  trades: AccountTrade[]
  failures: OperationFailure[]
  scope: string
  started_at: string
  sampled_at: string
  queried_at: string | null
  errors: string[]
  symbols: string[]
  limit: number
  truncated: boolean
}

export function getAccountActivity(accountId: number, symbol: string, limit: number, refresh: boolean, signal?: AbortSignal): Promise<AccountActivity> {
  const params = new URLSearchParams({ limit: String(limit), refresh: String(refresh) })
  if (symbol) params.set('symbol', symbol)
  return apiGet(`/account/${accountId}/orders/activity?${params}`, signal)
}
