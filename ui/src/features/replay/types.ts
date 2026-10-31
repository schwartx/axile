/** 回放保留本地观察微秒和录制段身份；字段语义由录制上下文固定。 */
export type ReplayData = Record<string, unknown>
export interface ReplayMessage {
  schema_version: number
  recording_id: string
  capture_seq: number
  time_us: number
  topic: string
  data: ReplayData
}
export interface ReplayRange { start_us: number; end_us: number }
export interface ReplayGap extends ReplayRange { reason?: string; topic?: string; symbol?: string }
export interface ReplayInstrument {
  price_tick?: number
  price_decimals?: number
  quantity_label?: string
  quantity_decimals?: number
  price_label?: string
  label?: string
}
export interface ReplayStream {
  availability: 'available' | 'partial' | 'unavailable'
  reason?: string
  source?: string
  delivery?: string
  available_depth?: number
  granularity?: string
}
export interface ReplayDisplayField {
  key: string
  label: string
  kind: 'text' | 'number' | 'bool' | 'enum'
  group?: string
  unit?: string
  decimals?: number
  values?: Record<string, string>
}
export interface ReplayContext {
  schema_version: number
  channel?: string
  account_mode?: string
  position_mode?: string
  environment?: string
  adapter_version?: string
  units?: { quantity_label?: string; quantity_max_decimals?: number; price_label?: string }
  instrument?: Record<string, ReplayInstrument | null>
  streams?: Record<string, Record<string, ReplayStream>>
  stream_defaults?: Record<string, ReplayStream>
  display?: { order?: ReplayDisplayField[]; trade?: ReplayDisplayField[]; position?: ReplayDisplayField[] }
}
export interface ReplayRecording {
  recording_id: string
  coverage: ReplayRange[]
  gaps?: ReplayGap[]
  boundary_reason?: string
  order_known?: boolean
  context: ReplayContext
}
export interface ReplayManifest {
  account_id: number
  execution_id: string
  availability: string
  reason?: string
  recordings: ReplayRecording[]
}
export interface ReplayWindow {
  recording_id: string
  window_id: string
  state_before_start: ReplayData | null
  messages: ReplayMessage[]
  next_cursor: string | null
  complete: boolean
  coverage: ReplayRange[]
  gaps: ReplayGap[]
  context: ReplayContext
}
export interface ReplayLevel { price: number; quantity: number }
export interface ReplayQuote {
  symbol: string
  bids: ReplayLevel[]
  asks: ReplayLevel[]
  book_valid: boolean
  valid_until_us: number | null
  available_depth: number
  depth_coverage: ReplayData | null
  last_price: number | null
  last_price_kind: string
  time_us: number
  capture_seq: number
}
export interface ReplayOrder {
  key: string
  order_id: string
  symbol: string
  price: number | null
  volume: number | null
  remaining_volume: number | null
  filled_volume: number | null
  direction: string | null
  offset: string | null
  status: string | null
  confirmed: boolean
  cancel_pending: boolean
  terminal: boolean
  carry_in: boolean
  execution_id: string | null
  previous_order_id: string | null
  time_us: number
  capture_seq: number
  attributes: ReplayData
  native_identity: ReplayData
}
export interface ReplayTrade {
  key: string | null
  symbol: string
  order_id: string | null
  price: number | null
  quantity: number | null
  direction: string | null
  offset: string | null
  position_side: string | null
  source: string | null
  source_seq: number | null
  execution_id: string | null
  identity_quality: string
  origin: string | null
  conflict: boolean
  granularity: string | null
  time_us: number
  capture_seq: number
  attributes: ReplayData
  native_identity: ReplayData
}
export interface ReplayCommand {
  command_id: string
  operation: string
  phase: string
  symbol: string | null
  order_id: string | null
  price: number | null
  volume: number | null
  time_us: number
  capture_seq: number
  data: ReplayData
}
export interface ReplayPosition {
  symbol: string
  direction: string | null
  volume: number | null
  available_volume: number | null
  frozen_volume: number | null
  today_volume: number | null
  yesterday_volume: number | null
  time_us: number
  attributes: ReplayData
}
export interface ReplayProjection {
  recording_id: string
  time_us: number
  capture_seq: number
  quotes: Record<string, ReplayQuote>
  orders: Record<string, ReplayOrder>
  orderHistory: ReplayOrder[]
  quoteHistory: ReplayQuote[]
  ownTrades: ReplayTrade[]
  marketTrades: ReplayTrade[]
  commands: ReplayCommand[]
  targets: Record<string, number>
  targetsByExecution: Record<string, Record<string, number>>
  observedPositions: Record<string, ReplayPosition>
  derivedPositions: Record<string, ReplayPosition>
  executionTotals: Record<string, Record<string, number>>
  executionComplete: Record<string, boolean>
  executionSymbolComplete: Record<string, Record<string, boolean>>
  accountingComplete: boolean
  warnings: string[]
}
export interface ReplayCursor { time_us: number; capture_seq?: number }
