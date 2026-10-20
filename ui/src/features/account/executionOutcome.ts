/** 只读取执行记录已有字段，不从错误文本推断机器态。BLOCKED 标题认 reason_code，不解析 error。 */
const SESSION_CLOSED = 'COMMON.SESSION.CLOSED'
type Dict = Record<string, unknown>
const dict = (value: unknown): Dict => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {}
const text = (value: unknown): string => typeof value === 'string' ? value : ''
const count = (value: unknown): number | null => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
/** 业务详情与分析快照摘要使用各自接口；平铺活动摘要直接读取已有字段。 */
const resultOf = (record: Dict): Dict => 'result' in record ? dict(record.result) : 'raw_result' in record ? dict(record.raw_result) : record
/** 主记录的明确空值仍是事实，不能被结果内较早的状态或原因覆盖。 */
const fieldOf = (record: Dict, result: Dict, key: string): unknown => Object.hasOwn(record, key) ? record[key] : result[key]
const displayFieldOf = (record: Dict, result: Dict, key: string): unknown => record[`display_${key}`] ?? fieldOf(record, result, key)
const affectedStates = new Set(['BLOCKED', 'PARTIAL', 'FAILED'])
export type ViewState = 'SUCCEEDED' | 'NOOP' | 'BLOCKED' | 'PARTIAL' | 'FAILED' | 'TERMINATED' | 'UNKNOWN'
export interface OutcomeView {
  state: ViewState
  title: string
  tone: 'neutral' | 'warn'
  reason: string
  symbolCount: number | null
  affectedCount: number
  tradeCount: number | null
  symbols: string[]
  text: string
  warning: boolean
  sessionScope: 'all' | 'partial' | null
}

function recordedReason(raw: Dict, results: Dict[]): string {
  const reason = text(raw.error)
  if (reason) return reason
  const affected = results.filter(result => affectedStates.has(text(result.status)))
  const reasons = affected.map(result => text(result.error))
  if (!reasons.some(Boolean)) return ''
  if (reasons.every(value => value === reasons[0])) return `${reasons[0]}${affected.length > 1 ? `，${affected.length} 个品种执行受阻` : ''}`
  return `${affected.length} 个品种执行受阻，原因不同`
}

function sessionScopeOf(state: ViewState, reasonCode: string, taskStatus: unknown, results: Dict[]): OutcomeView['sessionScope'] {
  if (!['BLOCKED', 'PARTIAL'].includes(state)) return null
  const closed = results.filter(result => result.status === 'BLOCKED')
  if (results.length && !closed.length) return null
  const sessionOnly = results.every(result => ['SUCCEEDED', 'NOOP'].includes(text(result.status)) || (
    result.status === 'BLOCKED' && (!result.reason_code || result.reason_code === SESSION_CLOSED)
    && !(Array.isArray(result.orders) && result.orders.length) && !(Array.isArray(result.trades) && result.trades.length)
  ))
  if (!sessionOnly) return null
  if (reasonCode !== SESSION_CLOSED) return taskStatus === 'SKIPPED' && state === 'BLOCKED' ? 'all' : null
  return state === 'PARTIAL' || results.length > closed.length ? 'partial' : 'all'
}

export function executionRecordView(value: unknown, clear = false): OutcomeView {
  const record = dict(value)
  const raw = resultOf(record)
  const entries = Object.entries(dict(raw.symbol_results)).map(([symbol, result]) => [symbol, dict(result)] as const)
  const results = entries.map(([, result]) => result)
  const symbols = entries.filter(([, result]) => affectedStates.has(text(result.status))).map(([symbol]) => symbol)
  const affectedCount = symbols.length || entries.length
  const taskStatus = fieldOf(record, raw, 'task_status')
  const status = text(displayFieldOf(record, raw, 'status'))
  const state: ViewState = taskStatus === 'TERMINATED' ? 'TERMINATED' : ['SUCCEEDED', 'NOOP', 'BLOCKED', 'PARTIAL', 'FAILED'].includes(status) ? status as ViewState : 'UNKNOWN'
  const isClear = clear || fieldOf(record, raw, 'execution_kind') === 'clear_positions'
  const reasonCode = text(fieldOf(record, raw, 'reason_code'))
  const sessionScope = sessionScopeOf(state, reasonCode, taskStatus, results)
  const blockedTitle = sessionScope === 'all'
    ? '休市跳过'
    : sessionScope === 'partial' ? '部分品种休市 · 执行未全部完成'
    : `未执行${affectedCount ? ` · ${affectedCount} 个品种执行受阻` : ''}`
  const title = {
    SUCCEEDED: isClear ? '清仓完成' : '调仓完成',
    NOOP: isClear ? '无需清仓' : '无需调仓',
    BLOCKED: blockedTitle,
    PARTIAL: sessionScope === 'partial' ? '部分品种休市 · 执行未全部完成' : '执行未全部完成',
    FAILED: '执行失败', TERMINATED: '执行已终止', UNKNOWN: '执行状态未知',
  }[state]
  const warning = (affectedStates.has(state) && sessionScope !== 'all') || state === 'TERMINATED'
  const symbolCount = count(record.symbol_count) ?? (raw.symbol_results != null && typeof raw.symbol_results === 'object' && !Array.isArray(raw.symbol_results) ? entries.length : null)
  const observedTrades = results.filter(result => Array.isArray(result.trades))
  const tradeCount = count(record.trade_count) ?? (observedTrades.length ? observedTrades.reduce((total, result) => total + (result.trades as unknown[]).length, 0) : null)
  return { state, title, text: title, tone: warning ? 'warn' : 'neutral', warning,
    reason: recordedReason({ ...raw, error: displayFieldOf(record, raw, 'error') }, results), symbols, symbolCount, affectedCount, sessionScope, tradeCount }
}

export const executionOutcome = executionRecordView

/** 摘要描述执行规模；结论单独由状态列显示。 */
export function executionRecordSummary(value: unknown, recordedTradeCount?: number | null): string {
  const record = dict(value)
  const raw = resultOf(record)
  const view = executionRecordView(value)
  const tradeCount = recordedTradeCount === undefined ? view.tradeCount : recordedTradeCount
  return [
    fieldOf(record, raw, 'execution_kind') === 'clear_positions' ? '清仓' : '调仓',
    view.symbolCount ? `涉及 ${view.symbolCount} 个品种` : '',
    tradeCount == null ? '成交信息未获取' : tradeCount ? `${tradeCount} 笔成交` : '未记录成交',
  ].filter(Boolean).join(' · ')
}

export function outcomeOf(value: unknown): ViewState { return executionRecordView({ status: value }).state }
export function symbolPreview(symbols: string[]): string { return symbols.length ? `${symbols.length} 个品种执行未成功` : '' }
