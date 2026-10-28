import { apiGet, apiSend } from '@/lib/api/client'
import type { AccountPerformance, PerformanceSettings } from '@/types/api'
import type { CostSummary, CostTrade } from '@/features/history/costs'
import type { ChartSelection } from '@/features/history/chartModel'

export type PerformanceRange = '30' | '90' | 'all'
export interface CostExecutionRow {
  key: string
  record: { id: number; execution_id: string | null; created_at: string; is_success: number; raw_result: { status?: string; task_status?: string; error?: string; reason_code?: string; execution_kind?: string; symbol_results?: Record<string, unknown> } }
  noop: boolean
  durationSec?: number | null
  symbolCount: number
  symbols?: string[]
  tradeCount?: number | null
  positionCount?: number | null
  positions?: Array<{ symbol: string; quantity: number; direction: string }> | null
  transactions?: TransactionPreview[]
  reason?: string
  attempts?: Array<{ symbol: string; planned: number | null; filled: number | null; reason: string }>
  summary: CostSummary
}
export interface TransactionPreview {
  symbol: string
  side: 'buy' | 'sell' | 'none'
  quantity: number
  price: number | null
  reference: number | null
  referenceSource: 'mid' | 'last' | null
  time: number
  endTime: number
  timeEstimated: boolean
  before: number | null
  target: number | null
  summary: CostSummary
}
export interface CostSymbolRow { lastTime?: number; symbol: string; summary: CostSummary; buy: number; sell: number; quantityIncomplete: boolean }
export interface CostPage<T = CostExecutionRow | CostSymbolRow | CostTrade> {
  data_until?: string | null
  version: string
  summary: CostSummary & { estimated: number }
  successful: number
  noop: number
  count: number
  data: T[]
  next_cursor: string | null
}
export interface CostQuery {
  version: string
  range: PerformanceRange
  dimension: 'execution' | 'symbol' | 'trade' | 'summary'
  sort?: 'time' | 'cost'
  day?: string
  start?: number
  end?: number
  record_id?: number
  symbol?: string
  symbol_search?: string
  side?: 'buy' | 'sell' | 'none'
  limit?: number
  cursor?: string
}
export function selectionQuery(selection: ChartSelection) {
  return selection?.kind === 'execution' ? { record_id: selection.recordId } : selection?.kind === 'day' ? { day: selection.day } : selection?.kind === 'interval' ? { start: selection.start, end: selection.end } : {}
}
export const refreshPerformance = (id: number) => apiSend('POST', `/account/analysis/${id}/refresh`)
export function getPerformanceCosts<T = CostExecutionRow | CostSymbolRow | CostTrade>(id: number, query: CostQuery, signal?: AbortSignal) {
  const params = new URLSearchParams(Object.entries(query).filter(([, value]) => value != null).map(([key, value]) => [key, String(value)]))
  return apiGet<CostPage<T>>(`/account/analysis/${id}/costs?${params}`, signal)
}

export function savePerformanceSettings(id: number, settings: PerformanceSettings) {
  return apiSend<PerformanceSettings>('PATCH', `/account/performance-settings/${id}`, settings)
}

/** Source revision and the last complete native calculation. */
export interface AnalysisMetadata {
  schema_version: 1
  status: 'empty' | 'pending' | 'ready' | 'stale' | 'failed'
  source_version: number
  version: string | null
  logic_version: string
  engine_version: string
  computed_at: string | null
  data_until: string | null
  error: string | null
  retry_at: number | null
}

/** Scalar table transport. Decoders preserve null, zero, precision and field presence. */
export interface AnalysisTable {
  columns: string[]
  rows: unknown[][]
}

export interface AnalysisExecutionSummary {
  record_id: number
  execution_id: string | null
  time_ms: number
  created_at: string | null
  is_success: number | null
  noop: boolean
  has_transactions: boolean
  duration_sec: number | null
  symbol_count: number | null
  position_count: number | null
  positions_present: boolean
  reason: string | null
  summary: CostSummary
}

export interface AnalysisChartResponse extends AnalysisChart {
  schema_version: 1
  account_id: number
}

/** A complete lightweight chart. It contains no execution details or holdings. */
export interface AnalysisChart {
  version: string
  range: PerformanceRange
  computed_at: string | null
  data_until: string | null
  result: Omit<AccountPerformance, 'executions'>
  daily_costs: Record<string, CostSummary>
  events: { time: string; tag: string; text: string; executionId: string | null }[]
  event_count: number
  execution_index: AnalysisExecutionSummary[]
  contributions?: ContributionObservation[]
}

export type AnalysisDisplay = AnalysisChart

export interface AnalysisDetailQuery {
  version: string
  record_id: number
  limit?: number
  offset?: number
}

export interface AnalysisExecutionDetail {
  schema_version: 1
  version: string
  record_id: number
  header: Record<string, unknown>
  summary: Record<string, unknown>
  tables: Record<'positions' | 'attempts' | 'transactions' | 'transaction_fees' | 'symbols', AnalysisTable>
  next_offset: number | null
  total: number
  next_offsets?: Partial<Record<'positions' | 'attempts' | 'transactions' | 'transaction_fees' | 'symbols', number | null>>
}

function analysisQuery(query: object): string {
  return new URLSearchParams(Object.entries(query).filter(([, value]) => value != null).map(([key, value]) => [key, String(value)])).toString()
}

export const getAnalysisStatus = (id: number, signal?: AbortSignal) => apiGet<AnalysisMetadata>(`/account/analysis/${id}/status`, signal)
export const getAnalysisChart = (id: number, snapshotId: string, range: PerformanceRange, signal?: AbortSignal) => apiGet<AnalysisChartResponse>(`/account/analysis/${id}?${analysisQuery({ version: snapshotId, range })}`, signal)
export const getAnalysisExecutionDetail = (id: number, query: AnalysisDetailQuery, signal?: AbortSignal) => {
  const { record_id: recordId, ...params } = query
  return apiGet<AnalysisExecutionDetail>(`/account/analysis/${id}/execution/${recordId}?${analysisQuery(params)}`, signal)
}

export interface ContributionObservation {
  idx: number
  symbol: string
  product: string
  currency: string | null
  gross_pnl: number | null
  fee: number
  fee_missing: number
  issues: number
  trades: number
}
