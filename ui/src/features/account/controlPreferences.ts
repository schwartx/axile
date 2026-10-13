export const DEFAULT_CONTROL_METRICS = ['account:place_order:per_day', 'account:cancel_order:per_day']
export const OPERATIONS: Record<string, string> = {
  place_order: '报单', cancel_order: '撤单', cancel_order_ctp: 'CTP 原生撤单', query_order: '查单',
  query_trades: '查询成交', query_orders: '查询委托', ctp_query_trades: '查询 CTP 成交',
  query_account: '查询资金', query_positions: '查询持仓', query_instruments: '查询合约',
  query_depth_market_data: '查询行情', query_settlement_info: '查询结算单',
  authenticate: '认证', trader_login: '交易登录', query_settlement_status: '查询结算确认', confirm_settlement: '确认结算',
  option_exercise: '期权行权', option_abandon: '期权放弃', option_self_close: '期权自对冲',
  cancel_option_exercise: '撤销期权行权', cancel_option_abandon: '撤销期权放弃', cancel_option_self_close: '撤销期权自对冲',
}
export const CONTROL_WINDOWS = [{ value: 'per_day', label: '今日' }, { value: 'per_minute', label: '本分钟' }] as const
export function operationName(key: string, names: Record<string, string> = {}): string {
  return names[key] ?? OPERATIONS[key] ?? key
}
export function metricLabel(id: string, compact = false, names: Record<string, string> = {}): string {
  const [, operation, window] = id.split(':')
  return `${window === 'per_minute' ? '本分钟' : compact ? '' : '今日'}${operationName(operation, names)}`
}
export function canAddMetric(draft: string[], id: string): boolean {
  return draft.length < 3 && !draft.includes(id)
}
export interface ControlCandidate { key: string; name: string; category: string }
/** 目录只补齐名称与分类；账户策略和实时账户规则决定候选范围。 */
export function controlCandidates(operations: { key: string; display_name: string; category: string }[], policyKeys: string[], rules: { scope: string; operation: string }[]): ControlCandidate[] {
  const keys = new Set([...policyKeys, ...rules.filter((rule) => rule.scope === 'account').map((rule) => rule.operation)])
  const common = ['place_order', 'cancel_order', 'query_order']
  const ordered = [...common, ...operations.map((operation) => operation.key), ...keys]
  const seen = new Set<string>()
  const rows: ControlCandidate[] = []
  for (const key of ordered) {
    if (!keys.has(key) || seen.has(key)) continue
    seen.add(key)
    const meta = operations.find((operation) => operation.key === key)
    rows.push({ key, name: operationName(key, meta ? { [key]: meta.display_name } : {}), category: common.includes(key) ? '常用' : meta?.category || '其他' })
  }
  const categories = [...new Set(rows.map((row) => row.category))]
  return categories.flatMap((category) => rows.filter((row) => row.category === category))
}
export function searchControlCandidates(rows: ControlCandidate[], query: string): ControlCandidate[] {
  const aliases: Record<string, string> = { place_order: '报单 下单 委托', cancel_order: '撤单 撤销委托', query_order: '查单 查询订单' }
  const needle = query.trim().toLowerCase()
  return rows.filter((row) => `${row.name} ${row.key} ${aliases[row.key] ?? ''}`.toLowerCase().includes(needle))
}

/** 兼容原有两项偏好，同时支持一至三项及固定分钟窗口。 */
export function normalizeControlPreferences(value: unknown): string[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 3
    || !value.every((key) => typeof key === 'string' && /^account:[\w-]+:(per_day|per_minute)$/.test(key))
    || new Set(value).size !== value.length) return [...DEFAULT_CONTROL_METRICS]
  return [...value]
}
export function readControlPreferences(accountId: number): string[] {
  try {
    return normalizeControlPreferences(JSON.parse(localStorage.getItem(`axile:control:${location.origin}:${accountId}`) ?? 'null'))
  } catch { return [...DEFAULT_CONTROL_METRICS] }
}
export function saveControlPreferences(accountId: number, value: string[]) {
  const normalized = normalizeControlPreferences(value)
  if (JSON.stringify(normalized) !== JSON.stringify(value)) return
  try { localStorage.setItem(`axile:control:${location.origin}:${accountId}`, JSON.stringify(value)) } catch { /* 当前会话仍生效。 */ }
}
