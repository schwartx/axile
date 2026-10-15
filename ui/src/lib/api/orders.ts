import { apiGet, apiSend } from '@/lib/api/client'

export interface CurrentOrder {
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
