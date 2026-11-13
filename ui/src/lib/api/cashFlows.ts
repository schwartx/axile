import { apiGet, apiSend } from '@/lib/api/client'

export interface CashFlowDraft {
  direction: 'deposit' | 'withdraw'
  amount: number
  occurred_at: string
  note: string
}
export interface CashFlow extends CashFlowDraft {
  id: number
  source: 'manual' | 'channel'
  status: 'active' | 'pending' | 'void'
  revision: number
  external_id: string | null
  manual_candidates?: { id: number; revision: number; occurred_at: string }[]
}
export interface CashFlowPage {
  data: CashFlow[]
  total: number
  currency: string
  sync_supported: boolean
}
export const getCashFlows = (id: number, offset: number, signal?: AbortSignal) =>
  apiGet<CashFlowPage>(`/account/${id}/cash-flows?limit=20&offset=${offset}`, signal)
export const saveCashFlow = (id: number, draft: CashFlowDraft, existing: CashFlow | null) => existing
  ? apiSend('PATCH', `/account/${id}/cash-flows/${existing.id}`, { ...draft, revision: existing.revision })
  : apiSend('POST', `/account/${id}/cash-flows`, draft)
export const resolveCashFlow = (id: number, flow: CashFlow, action: 'void' | 'confirm' | 'link', manual?: { id: number; revision: number }) =>
  apiSend('POST', `/account/${id}/cash-flows/${flow.id}/${action}`, { revision: flow.revision, ...(manual ? { manual_id: manual.id, manual_revision: manual.revision } : {}) })
export const syncCashFlows = (id: number) => apiSend<{ changed: number }>('POST', `/account/${id}/cash-flows/sync`)
