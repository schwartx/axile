import { ApiError } from '@/lib/api/client'
import { getAnalysisExecutionDetail, type AnalysisExecutionDetail, type AnalysisTable } from '@/lib/api/performance'
import { decodeAnalysisTable } from '@/features/history/analysisClient'

type Row = Record<string, unknown>
type Resource = keyof AnalysisExecutionDetail['tables']
export interface DetailSummary {
  value: number | null; cost: number | null; lossBp: number | null; coverage: number | null
  covered: number | null; count: number | null; amountComplete: boolean | null; feeCovered: number | null
  fees: Record<string, number>
}
export interface DetailPosition { idx: number; symbol: string | null; quantity: number | null; direction: string | null }
export interface DetailAttempt { idx: number; symbol: string | null; planned: number | null; filled: number | null; reason: string | null }
export interface DetailTransaction {
  idx: number; symbol: string | null; side: string | null; quantity: number | null; price: number | null
  reference: number | null; referenceSource: string | null; time: number | null; endTime: number | null
  timeEstimated: boolean | null; before: number | null; target: number | null; summary: DetailSummary
}
export interface ExecutionEvidence {
  recordId: number; executionId: string | null; createdAt: string | null; isSuccess: number | null
  rawResult: Row; noop: boolean | null; reason: string | null; summary: DetailSummary
  positions: DetailPosition[] | null; attempts: DetailAttempt[]; transactions: DetailTransaction[]
  positionsComplete: boolean; symbolsComplete: boolean; transactionTotal: number; nextOffset: number | null
}

const number = (value: unknown): number | null => {
  if (value == null) return null
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error('执行详情数值无效')
  return value
}
const text = (value: unknown): string | null => {
  if (value == null) return null
  if (typeof value !== 'string') throw new Error('执行详情文本无效')
  return value
}
const boolean = (value: unknown): boolean | null => {
  if (value == null) return null
  if (value === true || value === 1) return true
  if (value === false || value === 0) return false
  throw new Error('执行详情布尔值无效')
}
const integer = (value: unknown): number => {
  const result = number(value)
  if (result == null || !Number.isSafeInteger(result) || result < 0) throw new Error('执行详情序号无效')
  return result
}

function fees(value: unknown): Record<string, number> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  return Object.fromEntries(Object.entries(value).map(([currency, fee]) => {
    const amount = number(fee)
    if (amount == null) throw new Error('执行详情费用无效')
    return [currency, amount]
  }))
}

function summary(row: Row, currencyFees = fees(row.fees)): DetailSummary {
  return { value: number(row.value), cost: number(row.cost), lossBp: number(row.loss_bp), coverage: number(row.coverage),
    covered: number(row.covered), count: number(row.count), amountComplete: boolean(row.amount_complete),
    feeCovered: number(row.fee_covered), fees: currencyFees }
}

const RAW_FIELDS = ['status', 'task_status', 'error', 'outcome', 'outcome_reason', 'reason_code', 'msg', 'execution_kind'] as const
const SYMBOL_FIELDS = ['status', 'error', 'outcome', 'outcome_reason', 'reason_code'] as const
function presentFields(row: Row, fields: readonly string[], mask: unknown, prefix = ''): Row {
  const present = integer(mask)
  return Object.fromEntries(fields.flatMap((field, index) => present & (1 << index) ? [[field, text(row[prefix + field])]] : []))
}

export function detailNextOffset(response: AnalysisExecutionDetail, offset = -1): number | null {
  const candidates = [response.next_offset, ...Object.values(response.next_offsets ?? {})].filter(value => value != null).map(integer)
  if (!candidates.length) return null
  const next = Math.min(...candidates)
  if (next <= offset) throw new Error('执行详情分页未向前推进')
  return next
}

function transactions(response: AnalysisExecutionDetail): DetailTransaction[] {
  const byTransaction = new Map<number, Record<string, number>>()
  for (const row of decodeAnalysisTable<Row>(response.tables.transaction_fees, ['transaction_idx', 'currency', 'value'])) {
    const idx = integer(row.transaction_idx), currency = text(row.currency), value = number(row.value)
    if (currency == null || value == null) throw new Error('执行详情费用无效')
    const grouped = byTransaction.get(idx) ?? {}
    Object.defineProperty(grouped, currency, { value, enumerable: true, configurable: true, writable: true })
    byTransaction.set(idx, grouped)
  }
  return decodeAnalysisTable<Row>(response.tables.transactions, ['idx', 'symbol', 'quantity']).map(row => ({
    idx: integer(row.idx), symbol: text(row.symbol), side: text(row.side), quantity: number(row.quantity), price: number(row.price),
    reference: number(row.reference), referenceSource: text(row.reference_source), time: number(row.time), endTime: number(row.end_time),
    timeEstimated: boolean(row.time_estimated), before: number(row.before), target: number(row.target), summary: summary(row, byTransaction.get(integer(row.idx)) ?? {}),
  }))
}

/** Decode one selected execution only, retaining presence and incomplete-page evidence. */
export function decodeExecutionDetail(response: AnalysisExecutionDetail): ExecutionEvidence {
  if (response.schema_version !== 1) throw new Error('执行详情版本无效')
  const header = response.header
  const rawResult = presentFields(header, RAW_FIELDS, header.raw_present, 'raw_')
  if (boolean(header.has_symbol_results)) {
    rawResult.symbol_results = Object.fromEntries(decodeAnalysisTable<Row>(response.tables.symbols, ['symbol', 'present']).map(row => [
      text(row.symbol) ?? '', presentFields(row, SYMBOL_FIELDS, row.present),
    ]))
  }
  const positions = decodeAnalysisTable<Row>(response.tables.positions, ['idx', 'symbol', 'quantity', 'direction']).map(row => ({
    idx: integer(row.idx), symbol: text(row.symbol), quantity: number(row.quantity), direction: text(row.direction),
  }))
  return {
    recordId: integer(response.record_id), executionId: text(header.execution_id), createdAt: text(header.created_at), isSuccess: number(header.is_success),
    rawResult, noop: boolean(header.noop), reason: text(header.reason), summary: summary(response.summary),
    positions: boolean(header.positions_present) === true ? positions : null,
    positionsComplete: response.next_offsets?.positions == null, symbolsComplete: response.next_offsets?.symbols == null,
    attempts: decodeAnalysisTable<Row>(response.tables.attempts, ['idx', 'symbol', 'planned', 'filled', 'reason']).map(row => ({
      idx: integer(row.idx), symbol: text(row.symbol), planned: number(row.planned), filled: number(row.filled), reason: text(row.reason),
    })),
    transactions: transactions(response), transactionTotal: integer(response.total), nextOffset: detailNextOffset(response),
  }
}

const keys: Record<Resource, readonly string[]> = { positions: ['idx'], attempts: ['idx'], transactions: ['idx'], transaction_fees: ['transaction_idx', 'currency'], symbols: ['symbol'] }
function mergeTable(previous: AnalysisTable, page: AnalysisTable, resource: Resource): AnalysisTable {
  if (previous.columns.join('\0') !== page.columns.join('\0')) throw new Error('执行详情分页字段发生变化')
  const indexes = keys[resource].map(key => previous.columns.indexOf(key))
  if (indexes.some(index => index < 0)) throw new Error('执行详情缺少分页键')
  const rows = new Map<string, unknown[]>()
  for (const row of [...previous.rows, ...page.rows]) rows.set(JSON.stringify(indexes.map(index => row[index])), row)
  return { columns: previous.columns, rows: [...rows.values()] }
}

export function mergeExecutionDetail(previous: AnalysisExecutionDetail | null, page: AnalysisExecutionDetail): AnalysisExecutionDetail {
  if (!previous) return page
  if (previous.snapshot_id !== page.snapshot_id || previous.record_id !== page.record_id) throw new Error('执行详情版本不匹配')
  const tables = Object.fromEntries((Object.keys(keys) as Resource[]).map(resource => [resource, mergeTable(previous.tables[resource], page.tables[resource], resource)])) as AnalysisExecutionDetail['tables']
  return { ...page, tables }
}

export interface DetailState { data: ExecutionEvidence | null; loading: boolean; error: Error | null; expired: boolean }
export interface DetailEntry {
  accountId: number; snapshotId: string; recordId: number; state: DetailState
  payload: AnalysisExecutionDetail | null; listeners: Set<() => void>; flight: Promise<void> | null; controller: AbortController | null
}
export const EMPTY_DETAIL_STATE: DetailState = { data: null, loading: false, error: null, expired: false }
const cache = new Map<string, DetailEntry>()
const notify = (entry: DetailEntry, state: DetailState) => { entry.state = state; entry.listeners.forEach(listener => listener()) }
function trimCache() {
  for (const [key, entry] of cache) {
    if (cache.size <= 24) break
    if (!entry.flight && !entry.listeners.size) cache.delete(key)
  }
}
export function executionDetailEntry(accountId: number, snapshotId: string, recordId: number): DetailEntry {
  const key = JSON.stringify([accountId, snapshotId, recordId])
  const entry = cache.get(key) ?? { accountId, snapshotId, recordId, state: EMPTY_DETAIL_STATE, payload: null, listeners: new Set(), flight: null, controller: null }
  cache.delete(key); cache.set(key, entry); trimCache()
  return entry
}

/** Requests and cache entries are bound to the displayed snapshot and selected record. */
export function loadExecutionDetail(entry: DetailEntry, more = false, retry = false): Promise<void> {
  if (entry.flight) return entry.controller?.signal.aborted ? entry.flight.then(() => loadExecutionDetail(entry, more, retry)) : entry.flight
  if (entry.state.data && !more && !retry) return Promise.resolve()
  if (more && (entry.state.data?.nextOffset == null || entry.state.expired)) return Promise.resolve()
  const offset = more ? entry.state.data!.nextOffset! : 0
  const controller = new AbortController()
  entry.controller = controller
  notify(entry, { ...entry.state, loading: true, error: null, expired: false })
  entry.flight = (async () => {
    try {
      const page = await getAnalysisExecutionDetail(entry.accountId, { snapshot_id: entry.snapshotId, record_id: entry.recordId, limit: 200, offset }, controller.signal)
      if (controller.signal.aborted) return
      if (page.snapshot_id !== entry.snapshotId || page.record_id !== entry.recordId) throw new Error('执行详情版本不匹配')
      detailNextOffset(page, offset)
      const payload = mergeExecutionDetail(more ? entry.payload : null, page)
      const data = decodeExecutionDetail(payload)
      entry.payload = payload
      notify(entry, { data, loading: false, error: null, expired: false })
    } catch (error) {
      if (!controller.signal.aborted) notify(entry, { ...entry.state, loading: false, error: error instanceof Error ? error : new Error(String(error)), expired: error instanceof ApiError && error.status === 410 })
    } finally {
      entry.flight = null; entry.controller = null
      if (entry.state.loading) notify(entry, { ...entry.state, loading: false })
      trimCache()
    }
  })()
  return entry.flight
}
