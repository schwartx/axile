import { currentHoldingPreview, type CurrentHoldingPreview } from '@/features/account/holdingPreview'
import type { CancelReceipt, CurrentOrder, CurrentOrdersSnapshot } from '@/lib/api/orders'
import type { Position, TargetSizingRow, TargetWeightSnapshot } from '@/types/api'

const EPS = 1e-9
const CN_EXCHANGES = new Set(['SHFE', 'DCE', 'CZCE', 'CFFEX', 'INE', 'GFEX'])

/** 只接受已知国内交易所的限定别名；两个不同交易所绝不合并。 */
export function sameHoldingSymbol(left: string, right: string): boolean {
  if (left === right) return true
  const qualified = (symbol: string) => {
    const [exchange, ...rest] = symbol.split('.')
    return CN_EXCHANGES.has(exchange) && rest.length === 1 ? rest[0] : null
  }
  return (!left.includes('.') && qualified(right) === left)
    || (!right.includes('.') && qualified(left) === right)
}

function symbolEntry<T>(values: Record<string, T> | null | undefined, symbol: string): T | undefined {
  if (!values) return undefined
  if (Object.hasOwn(values, symbol)) return values[symbol]
  const matches = Object.keys(values).filter(key => sameHoldingSymbol(key, symbol))
  return matches.length === 1 ? values[matches[0]] : undefined
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function nonnegative(value: unknown): number | null {
  const number = finite(value)
  return number != null && number >= 0 ? number : null
}

export interface HoldingPreviewRow extends CurrentHoldingPreview {
  targetOnly?: boolean
}

/** 已有持仓保留多空分行，追加尚未持有的非零目标，排序沿用持仓货值。 */
export function holdingPreviewRows(positions: Position[], equity: number, target: TargetWeightSnapshot | null, observed: boolean): HoldingPreviewRow[] {
  const rows: HoldingPreviewRow[] = currentHoldingPreview(positions, equity)
  const weights = target?.account_weights ?? target?.weights ?? {}
  for (const [symbol, weight] of Object.entries(weights)) {
    if (!Number.isFinite(weight) || Math.abs(weight) <= EPS || rows.some(row => sameHoldingSymbol(row.symbol, symbol))) continue
    rows.push({ key: `${symbol}:target`, symbol, direction: weight < 0 ? 'short' : 'long',
      weight: observed ? 0 : null, volume: observed ? 0 : null, availableVolume: observed ? 0 : null,
      value: null, targetOnly: true })
  }
  return rows
}

export interface SideHolding {
  volume: number | null
  available: number | null
  today: number | null
  yesterday: number | null
}

export interface HoldingBook {
  long: SideHolding
  short: SideHolding
}

function positionIsShort(position: Position): boolean {
  return typeof position.direction === 'string' && /空|short/i.test(position.direction)
}

/** 今昨仓只读取统一模型保存的分项，不从可用量或净仓倒推。 */
export function holdingBook(positions: Position[], symbol: string, observed: boolean): HoldingBook {
  const side = (short: boolean): SideHolding => {
    const rows = positions.filter(row => row.symbol && sameHoldingSymbol(row.symbol, symbol) && positionIsShort(row) === short)
    const sum = (read: (position: Position) => unknown): number | null => {
      if (!observed) return null
      const values = rows.map(row => nonnegative(read(row)))
      return values.some(value => value == null) ? null : values.reduce<number>((total, value) => total + (value ?? 0), 0)
    }
    const extra = (position: Position, key: string): unknown =>
      position.extra && typeof position.extra === 'object' ? (position.extra as Record<string, unknown>)[key] : undefined
    return { volume: sum(row => row.volume), available: sum(row => row.available_volume),
      today: sum(row => extra(row, short ? 'short_td' : 'long_td')),
      yesterday: sum(row => extra(row, short ? 'short_yd' : 'long_yd')) }
  }
  return { long: side(false), short: side(true) }
}

export interface HoldingTarget {
  weight: number | null
  quantity: number | null
  evidence: TargetSizingRow | null
}

/** 目标只认冻结的换算结果；对照接口按行情估算的数量不充当执行记录。 */
export function holdingTarget(target: TargetWeightSnapshot | null, symbol: string): HoldingTarget {
  if (!target?.calculated_at) return { weight: null, quantity: null, evidence: null }
  const weight = finite(symbolEntry(target.account_weights ?? target.weights, symbol) ?? 0)
  const evidence = symbolEntry(target.sizing?.rows, symbol) ?? null
  if (weight === 0) return { weight, quantity: 0, evidence }
  if (target.sizing?.status !== 'available') return { weight, quantity: null, evidence }
  return { weight, quantity: finite(evidence?.target_quantity ?? symbolEntry(target.quantities, symbol)), evidence }
}

export interface HoldingAction {
  kind: '买开' | '卖开' | '买平' | '卖平'
  quantity: number
}

export function holdingActions(book: HoldingBook, target: number | null): HoldingAction[] | null {
  if (target == null || book.long.volume == null || book.short.volume == null) return null
  const longDelta = Math.max(0, target) - book.long.volume
  const shortDelta = Math.max(0, -target) - book.short.volume
  const actions: HoldingAction[] = []
  // 反向时先展示平仓，再展示开仓；仅解释数量差，不代替执行器的可执行计划。
  if (longDelta < -EPS) actions.push({ kind: '卖平', quantity: -longDelta })
  if (shortDelta < -EPS) actions.push({ kind: '买平', quantity: -shortDelta })
  if (longDelta > EPS) actions.push({ kind: '买开', quantity: longDelta })
  if (shortDelta > EPS) actions.push({ kind: '卖开', quantity: shortDelta })
  return actions
}

export interface HoldingProjection {
  book: HoldingBook | null
  actions: HoldingAction[] | null
  reason: string | null
  effect: 'aligned' | 'closer' | 'farther' | 'overshoot' | 'unchanged' | null
}

type ProjectionInput = {
  book: HoldingBook
  target: number | null
  orders: CurrentOrdersSnapshot | null
  receipts: CancelReceipt[]
  observedAt: string | null
  stale: boolean
}

function observationIssue({ orders, receipts, observedAt, stale }: ProjectionInput): string | null {
  if (!orders || stale || !orders.complete) return '委托查询不完整或已过期'
  if (orders.data.some(order => order.uncertain || !['待成交', '已报', '部分成交'].includes(order.status))) return '部分委托或撤单结果尚未确认'
  if (receipts.some(receipt => ['submitting', 'unknown'].includes(receipt.state))) return '部分委托或撤单结果尚未确认'
  if (receipts.some(receipt => receipt.state === 'terminal' && orders.data.some(order =>
    order.order_id === receipt.order_id && order.symbol === receipt.symbol))) return '委托列表与撤单终态回执尚未同步'
  const observed = observedAt ? Date.parse(observedAt) : NaN
  const sampled = Date.parse(orders.sampled_at)
  if (!Number.isFinite(observed) || !Number.isFinite(sampled) || observed !== sampled) {
    return '持仓与委托采样时间不同，无法确认成交是否已计入持仓'
  }
  return null
}

function projectionEffect(book: HoldingBook, projected: HoldingBook, target: number | null): HoldingProjection['effect'] {
  const before = holdingActions(book, target)
  const after = holdingActions(projected, target)
  if (!before || !after || target == null) return null
  if (!after.length) return 'aligned'
  const crossed = (side: 'long' | 'short', goal: number) => {
    const current = book[side].volume
    const next = projected[side].volume
    return current != null && next != null && (current - goal) * (next - goal) < -EPS
  }
  if (crossed('long', Math.max(0, target)) || crossed('short', Math.max(0, -target))) return 'overshoot'
  const distance = (items: HoldingAction[]) => items.reduce((sum, item) => sum + item.quantity, 0)
  const difference = distance(after) - distance(before)
  if (difference > EPS) return 'farther'
  return difference < -EPS ? 'closer' : 'unchanged'
}

function applyRemaining(book: HoldingBook, order: CurrentOrder): boolean {
  const remaining = nonnegative(order.remaining_volume)
  if (remaining == null) return false
  if (remaining <= EPS) return true
  const buy = ['BUY', '买入'].includes(order.direction)
  if (!buy && !['SELL', '卖出'].includes(order.direction)) return false
  const open = ['0', 'OPEN'].includes(order.offset ?? '')
  const close = ['1', '3', '4', 'CLOSE', 'CLOSETODAY', 'CLOSEYESTERDAY'].includes(order.offset ?? '')
  if (!open && !close) return false
  const side = open ? (buy ? book.long : book.short) : (buy ? book.short : book.long)
  if (side.volume == null) return false
  side.volume += open ? remaining : -remaining
  return true
}

/** 没有同一时刻的观测证据时，宁可待确认，也不把已成交部分再次加回。 */
export function projectHolding(input: ProjectionInput): HoldingProjection {
  const { book, target, orders } = input
  const unknown = (reason: string): HoldingProjection => ({ book: null, actions: null, reason, effect: null })
  if (book.long.volume == null || book.short.volume == null) return unknown('持仓数量尚未确认')
  const issue = observationIssue(input)
  if (issue || !orders) return unknown(issue ?? '委托查询尚未完成')
  const projected: HoldingBook = { long: { ...book.long }, short: { ...book.short } }
  if (!orders.data.every(order => applyRemaining(projected, order))) return unknown('委托的买卖开平或剩余数量不完整')
  if ((projected.long.volume ?? 0) < -EPS || (projected.short.volume ?? 0) < -EPS) return unknown('在途平仓数量超过已观测持仓，需重新核对')
  const actions = holdingActions(projected, target)
  return { book: projected, actions, reason: null, effect: projectionEffect(book, projected, target) }
}
