import { browserAnalysis } from '@/features/history/browserAnalysis'
import { apiSend } from '@/lib/api/client'
import type { AccountPerformance, PerformanceCalendar, PerformanceSettings } from '@/types/api'
import type { CostSummary, CostTrade } from '@/features/history/costs'
import type { ChartSelection } from '@/features/history/chartModel'

export type PerformanceRange = '30' | '90' | 'all'
export interface PerformanceSnapshot {
  status: 'empty' | 'pending' | 'ready' | 'stale' | 'failed'
  source_version: number
  snapshot_id: string | null
  snapshot_version: number | null
  logic_version: string
  engine_version: string
  computed_at: string | null
  data_until: string | null
  settings: PerformanceSettings | null
  error: string | null
  retry_at: number | null
  result: AccountPerformance | null
  daily_costs: Record<string, CostSummary>
  events: { time: string; tag: string; text: string; executionId: string | null }[]
  event_count: number
}
export interface CostExecutionRow {
  key: string
  record: { id: number; execution_id: string | null; created_at: string; is_success: number; raw_result: { status?: string; task_status?: string; error?: string; reason_code?: string; execution_kind?: string; symbol_results?: Record<string, unknown> } }
  noop: boolean
  durationSec?: number | null
  symbolCount: number
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
  snapshot_id: string
  summary: CostSummary & { estimated: number }
  successful: number
  noop: number
  count: number
  data: T[]
  next_cursor: string | null
}
export interface CostQuery {
  snapshot_id: string
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
export const refreshPerformance = (id: number) => browserAnalysis<AnalysisMetadata>(id, 'refresh').then(metadata => { if (metadata.status === 'failed') throw new Error(metadata.error ?? '浏览器分析失败'); return metadata })
export function getPerformanceCosts<T = CostExecutionRow | CostSymbolRow | CostTrade>(id: number, query: CostQuery, signal?: AbortSignal) {
  return browserAnalysis<CostPage<T>>(id, 'costs', query, signal)
}

export function savePerformanceSettings(id: number, settings: PerformanceSettings) {
  return apiSend<PerformanceSettings>('PATCH', `/account/performance-settings/${id}`, settings)
}

/** Small mutable publication state; its ID must never replace a displayed chart ID. */
export interface AnalysisMetadata extends Omit<PerformanceSnapshot, 'result' | 'daily_costs' | 'events' | 'event_count' | 'settings'> {
  schema_version: 1
  read_view_schema_version: number
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

export interface AnalysisHeader extends Omit<AccountPerformance, 'settings' | 'points' | 'executions' | 'bindings' | 'calendar' | 'sizing_fallback_ranges'> {
  event_count: number
  calendar: Pick<PerformanceCalendar, 'status' | 'calendar_id' | 'label'>
}

export interface AnalysisChartResponse {
  schema_version: 1
  snapshot_id: string
  account_id: number
  range: PerformanceRange
  snapshot_version: number
  computed_at: string | null
  data_until: string | null
  settings: PerformanceSettings
  header: AnalysisHeader
  tables: { contributions?: AnalysisTable } & Record<'points' | 'bindings' | 'fallbacks' | 'calendar_ranges' | 'daily_costs' | 'daily_fees' | 'events' | 'execution_index' | 'execution_fees', AnalysisTable>
}

/** A complete lightweight chart. It contains no execution details or holdings. */
export interface AnalysisChart {
  snapshot_id: string
  snapshot_version: number
  range: PerformanceRange
  computed_at: string | null
  data_until: string | null
  result: Omit<AccountPerformance, 'executions'>
  daily_costs: Record<string, CostSummary>
  events: PerformanceSnapshot['events']
  event_count: number
  execution_index: AnalysisExecutionSummary[]
  contributions?: ContributionObservation[]
}

export type AnalysisDisplay = AnalysisChart

export interface AnalysisSummaryQuery {
  snapshot_id: string
  range: PerformanceRange
  limit?: number
  offset?: number
  start?: number
  end?: number
}

export interface AnalysisSummaryResponse {
  schema_version: 1
  snapshot_id: string
  range: PerformanceRange
  table: AnalysisTable
  fees: AnalysisTable
  next_offset: number | null
}

export interface AnalysisDetailQuery {
  snapshot_id: string
  record_id: number
  limit?: number
  offset?: number
}

export interface AnalysisExecutionDetail {
  schema_version: 1
  snapshot_id: string
  record_id: number
  header: Record<string, unknown>
  summary: Record<string, unknown>
  tables: Record<'positions' | 'attempts' | 'transactions' | 'transaction_fees' | 'symbols', AnalysisTable>
  next_offset: number | null
  total: number
  next_offsets?: Partial<Record<'positions' | 'attempts' | 'transactions' | 'transaction_fees' | 'symbols', number | null>>
}

export const getAnalysisStatus = (id: number, signal?: AbortSignal) => browserAnalysis<AnalysisMetadata>(id, 'status', {}, signal)
export const getAnalysisChart = (id: number, snapshotId: string, range: PerformanceRange, signal?: AbortSignal) => browserAnalysis<AnalysisChartResponse>(id, 'chart', { snapshot_id: snapshotId, range }, signal)
export const getAnalysisExecutionSummary = (id: number, query: AnalysisSummaryQuery, signal?: AbortSignal) => browserAnalysis<AnalysisSummaryResponse>(id, 'execution-summary', query, signal)
export const getAnalysisExecutionDetail = (id: number, query: AnalysisDetailQuery, signal?: AbortSignal) => browserAnalysis<AnalysisExecutionDetail>(id, 'execution-detail', query, signal)

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
