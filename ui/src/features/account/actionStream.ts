import { executionOutcome } from '@/features/account/executionOutcome'
/**
 * 把执行事件流铺成每品种的「动作流」——因果排序的动词时间线（纯函数，便于测试）。
 *
 * 详情页的「结论层」（头条 + 逐只到位 + TCA）回答「结果对不对」，是把过程坍缩成的状态；
 * 本模块回答「过程发生了什么」，把同一批事件保留成流的形态：决策 → 挂单 → 追价 → 成交。
 *
 * 一次执行是「1 条控制面 + N 条并行品种工作流」，不是一根时间轴。故按品种分道，每道内
 * 按**因果次序**排：决策固定置顶（`symbol_decision_made` 是执行末尾补发的，时间戳偏晚，
 * 按时间排会掉到成交后面），其余按 (ts, seq)。追价撤旧单的终态事件按 `prev_order_id`
 * 归并抑制，避免每次追价都甩一行「撤销」噪声。
 */
import { eventError } from '@/features/account/executionRows'
import { executionReasonText } from '@/features/account/executionReason'
import { displayCurrencyUnit } from '@/lib/format'
import type { ExecutionEvent } from '@/types/api'

/** 动作流的一行：一条事件渲染成的动词句。 */
export interface ActionLine {
  key: string
  /** `HH:MM:SS`；无时间时为空串。 */
  time: string
  /** 关联品种；生命周期事件为 null（骨干行）。 */
  symbol: string | null
  /** 动词短语文案。 */
  text: string
  status: 'INFO' | 'SUCCESS' | 'WARNING' | 'ERROR'
  /** 断点（失败/跳过/撤销/拒单）：供 UI 变红。 */
  broken: boolean
  /** 正向确认里程碑（成交/对账成功）：供 UI 上 accent 蓝，与「琥珀断点/中性过程」分三态。 */
  good: boolean
}

/** 渠道声明的动作流数量与价格展示单位。 */
export interface ActionDisplayUnits {
  quantityLabel?: string
  quantityMaxDecimals?: number
  priceLabel?: string
}

type Dict = Record<string, unknown>

function asDict(v: unknown): Dict | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Dict) : null
}

function asNum(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function asStr(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/** 秒级 ISO 本地时间戳 → `HH:MM:SS`。 */
function hhmmss(ts: string): string {
  const t = ts.includes('T') ? ts.split('T')[1] : ts
  return (t ?? '').slice(0, 8)
}

/** 带号数量：保留精度、裁掉尾随零；空值为空串。 */
function fmtQty(v: number | null, units: ActionDisplayUnits): string {
  if (v == null) return ''
  const value = v.toLocaleString('zh-CN', { maximumFractionDigits: units.quantityMaxDecimals ?? 6 })
  return units.quantityLabel ? `${value} ${units.quantityLabel}` : value
}

/** 价格按通用精度格式化，并在渠道声明时追加报价单位。 */
function fmtPrice(v: number | null, units: ActionDisplayUnits): string {
  if (v == null) return ''
  const value = v.toLocaleString('zh-CN', { maximumFractionDigits: 6 })
  return units.priceLabel ? `${value} ${displayCurrencyUnit(units.priceLabel)}` : value
}

/** 订单方向枚举串（`OrderDirection.SELL`）→ 买/卖/空。 */
function sideText(direction: string): string {
  const d = direction.toUpperCase()
  if (d.includes('SELL') || direction.includes('卖')) return '卖'
  if (d.includes('BUY') || direction.includes('买')) return '买'
  return ''
}

/** 终态串归类：成交 / 撤销 / 拒单 / 过期。只认统一中文终态，避免 FILLED/部分成交被 includes 误判成全部成交。 */
function terminalKind(status: string): 'filled' | 'canceled' | 'rejected' | 'expired' | 'other' {
  if (status === '已成交') return 'filled'
  if (status === '已撤销') return 'canceled'
  if (status === '已拒绝') return 'rejected'
  if (status === '已过期') return 'expired'
  return 'other'
}

/** 执行对账终态枚举 → 人话。 */
const COMPLETION_LABEL: Record<string, string> = {
  SUCCEEDED: '成功',
  FAILED: '失败',
  PARTIAL: '部分成功',
  BLOCKED: '受阻',
  NOOP: '空跑',
}

/** 单条 `symbol_decision_made` → 文案（目标仓 + 算法 + 单数，失败带原因）。 */
function decisionText(e: ExecutionEvent, units: ActionDisplayUnits): string {
  const d = asDict(asDict(e.details)?.decision)
  const target = fmtQty(asNum(d?.target_volume), units)
  const algorithm = asStr(d?.algorithm)
  const orders = asNum(d?.orders_count)
  const parts: string[] = ['决策']
  if (target) parts.push(`目标仓 ${target}`)
  if (algorithm) parts.push(algorithm)
  if (orders != null) parts.push(`${orders} 单`)
  let text = parts.join(' · ')
  if (d?.status != null) return `${text} · ${executionOutcome(d).text}`
  const err = eventError(e)
  if (err) text += ` · ${err}`
  return text
}

/** 单条 `order_submitted` → 文案（普通挂单 / 市价兜底）。 */
function submitText(e: ExecutionEvent, units: ActionDisplayUnits): string {
  const details = asDict(e.details)
  const fallback = asDict(details?.fallback)
  if (fallback) {
    const side = sideText(asStr(fallback.direction))
    const vol = fmtQty(asNum(fallback.remaining_volume), units)
    return `市价兜底挂单${side ? ` ${side}` : ''}${vol ? ` ${vol}` : ''}`
  }
  const order = asDict(details?.order)
  const side = sideText(asStr(order?.direction))
  const vol = fmtQty(asNum(order?.volume), units)
  const isMarket = asStr(order?.order_type).toUpperCase() === 'MARKET'
  const price = fmtPrice(asNum(order?.price), units)
  const tail = [side, vol].filter(Boolean).join(' ')
  if (isMarket) return `市价下单${tail ? ` ${tail}` : ''}`
  return `挂单${tail ? ` ${tail}` : ''}${price ? ` @${price}` : ''}`
}

/** 单条追价（`order_submitted` + `COMMON.ORDER_CHASE`）→ 文案。 */
function chaseText(e: ExecutionEvent, units: ActionDisplayUnits): string {
  const c = asDict(asDict(e.details)?.chase)
  const to = fmtPrice(asNum(c?.to_price), units)
  const idx = asNum(c?.index)
  const max = asNum(c?.max)
  const seq = idx != null ? `（第 ${idx}${max != null ? `/${max}` : ''} 次）` : ''
  return `追价${to ? ` → @${to}` : ''}${seq}`
}

/** 判定一条 `order_submitted` 是否为追价（带 `details.chase` 或专属 reason_code）。 */
function isChase(e: ExecutionEvent): boolean {
  return asDict(e.details)?.chase != null || e.reason_code === 'COMMON.ORDER_CHASE'
}

/** 单条 `order_terminal` → 文案（成交/撤销/拒单/过期 + 成交量与均价）。 */
function terminalText(e: ExecutionEvent, units: ActionDisplayUnits): { text: string; broken: boolean; good?: boolean } {
  const order = asDict(asDict(e.details)?.order)
  const side = sideText(asStr(order?.direction))
  const status = asStr(order?.terminal_status || order?.status)
  const kind = terminalKind(status)
  const filled = fmtQty(asNum(order?.filled_volume), units)
  const volume = fmtQty(asNum(order?.volume), units)
  const avg = fmtPrice(asNum(order?.avg_price), units)
  const qty = filled && volume ? `${filled}/${volume}` : filled || volume
  const priceTail = avg && avg !== '0' ? ` @${avg}` : ''
  switch (kind) {
    case 'filled':
      return { text: `成交${side ? ` ${side}` : ''}${qty ? ` ${qty}` : ''}${priceTail}`, broken: false, good: true }
    case 'canceled':
      return { text: `撤销${filled && filled !== '0' ? `（已成 ${filled}）` : ''}`, broken: true }
    case 'rejected':
      return { text: '拒单', broken: true }
    case 'expired':
      return { text: '过期', broken: true }
    default:
      return { text: status ? `终态 ${status}` : '终态', broken: e.status === 'ERROR' }
  }
}

/** 解释当次平仓计划，预设费用说明不冒充实际账户费率。 */
function closePolicyText(e: ExecutionEvent): string {
  const d = asDict(asDict(e.details)?.futures_close_decision)
  if (!d) return '平仓规则'
  if (d.source === 'exchange') return `平仓规则 · ${asStr(d.reason) || '按交易所规则执行'}`
  const priority = d.priority === 'today_first' ? '先今后昨' : '先昨后今'
  const sources: Record<string, string> = { fee: '低手续费优先', preset: '品种预设', override: '人工覆盖', default: '默认顺序' }
  const parts = [`平仓规则 · ${priority}`, sources[asStr(d.source)] || '按品种规则']
  const yesterday = asNum(d.yesterday_cost), today = asNum(d.today_cost)
  if (yesterday != null && today != null) parts.push(`预计平昨 ${yesterday.toFixed(2)}／平今 ${today.toFixed(2)} 元/手`)
  if (asStr(d.fallback_reason)) parts.push(asStr(d.fallback_reason))
  if (asStr(d.preset_version)) parts.push(`预设 ${asStr(d.preset_version)}`)
  const legs = Array.isArray(d.legs) ? d.legs.map((leg) => {
    const row = asDict(leg)
    const name = row?.offset_flag === 'close_today' ? '平今' : row?.offset_flag === 'close_yesterday' ? '平昨' : '平仓'
    return `${name} ${asNum(row?.volume) ?? '—'} 手`
  }) : []
  if (legs.length) parts.push(legs.join(' → '))
  return parts.join(' · ')
}

/** 一条事件 → 动作流文案与断点标记（symbol=null 为生命周期骨干行）。 */
function lineOf(e: ExecutionEvent, units: ActionDisplayUnits): { text: string; broken: boolean; good?: boolean } {
  const err = eventError(e)
  switch (e.event_type) {
    case 'execution_started':
      return { text: '触发', broken: false }
    case 'input_snapshotted': {
      const n = asNum(asDict(asDict(e.details)?.debug)?.symbol_count)
      return { text: n != null ? `冻结输入 · ${n} 品种` : '冻结输入', broken: false }
    }
    case 'target_computed':
      return { text: '算出目标', broken: false }
    case 'futures_close_planned':
      return { text: closePolicyText(e), broken: false }
    case 'symbol_decision_made':
      return { text: decisionText(e, units), broken: e.status === 'ERROR' }
    case 'symbol_skipped':
      return { text: `跳过 · ${err || executionReasonText(e.reason_code, '未执行')}`, broken: true }
    case 'order_submitted':
      return isChase(e) ? { text: chaseText(e, units), broken: false } : { text: submitText(e, units), broken: false }
    case 'order_acknowledged':
      return { text: '已受理', broken: false }
    case 'order_terminal':
      return terminalText(e, units)
    case 'execution_completed': {
      const st = asStr(asDict(asDict(e.details)?.debug)?.execution_status)
      return { text: `对账 · ${COMPLETION_LABEL[st] ?? '完成'}`, broken: false, good: st === 'SUCCEEDED' }
    }
    case 'execution_failed':
      return { text: err || executionReasonText(e.reason_code, '执行失败'), broken: true }
    case 'execution_terminated':
      return { text: '已终止', broken: true }
    case 'execution_termination_requested':
      return { text: '请求终止', broken: false }
    case 'execution_termination_acked':
      return { text: '确认终止', broken: false }
    default:
      return { text: err || executionReasonText(e.reason_code, '执行事件'), broken: e.status === 'ERROR' }
  }
}

/** 事件因果 rank：决策/跳过为 0（置顶），订单级为 1（其内按时间排）。 */
function causalRank(e: ExecutionEvent): number {
  return e.event_type === 'symbol_decision_made' || e.event_type === 'symbol_skipped' ? 0 : 1
}

/**
 * 铺出单个品种的因果动作流.
 *
 * @param events - 整条执行的事件流（`GET .../events`）。
 * @param symbol - 目标品种。
 * @param units - 渠道声明的数量与价格展示单位。
 * @returns 该品种的动作行：决策置顶，其余按 (ts, seq)，追价撤旧单终态已抑制。
 */
export function buildSymbolActionStream(
  events: ExecutionEvent[],
  symbol: string,
  units: ActionDisplayUnits = {},
): ActionLine[] {
  const scoped = events.filter((e) => e.symbol === symbol)

  // 追价重挂的旧单会各自收到一条「撤销」终态；按 prev_order_id 抑制，只留真实成交/失败。
  const superseded = new Set<string>()
  for (const e of scoped) {
    const prev = asDict(asDict(e.details)?.chase)?.prev_order_id
    if (typeof prev === 'string') superseded.add(prev)
  }
  const visible = scoped.filter(
    (e) => !(e.event_type === 'order_terminal' && e.order_id != null && superseded.has(e.order_id)),
  )

  const sorted = visible.sort((a, b) => {
    if (causalRank(a) !== causalRank(b)) return causalRank(a) - causalRank(b)
    const ta = Date.parse(a.ts_local_created)
    const tb = Date.parse(b.ts_local_created)
    if (!Number.isNaN(ta) && !Number.isNaN(tb) && ta !== tb) return ta - tb
    return a.seq - b.seq
  })

  return sorted.map((e, i) => {
    const { text, broken, good } = lineOf(e, units)
    return { key: `${e.seq}-${i}`, time: hhmmss(e.ts_local_created), symbol: null, text, status: e.status, broken, good: good ?? false }
  })
}
