/**
 * 从后端原始数据派生 UI 语义。
 *
 * 后端不下发「到位/背离」verdict，也无手续费汇总（见方案已知缺口）；这里用
 * 现有字段做诚实的粗粒度派生：能确定的说确定，不能确定的不编。
 */
import { formatPlannedAt } from '@/lib/scheduleTime'
import type { AccountAssets, AccountDashboardItem, AccountRebalancePlan, ExecuteRecord, LatestWeights, Position } from '@/types/api'

/**
 * 规整后端返回的账户计价货币.

 * Parameters
 * ----------
 * currency : string | null | undefined
 *     后端账户或资产快照返回的计价货币。

 * Returns
 * -------
 * str
 *     渠道声明的计价货币代码，拿不到渠道时返回 ``''``。
 */
export function currencyOf(currency: string | null | undefined): string {
  return currency?.trim() ?? ''
}

export type StatusLevel = 'ok' | 'warn' | 'bad' | 'run'

/* ==================== 账户两轴：在位性 ⊥ 档位 ==================== */

/**
 * 在位性轴：账户此刻在不在你要它待的位置（观察到的现实）。
 *
 * 「风险」轴——钱对不对得上意图，是新闻、可行动、该不可遮。与档位（自动/暂停）正交：
 * 账户可以暂停且偏离；主动暂停时，偏离作为中性事实保留，不要求立即调仓。
 */
export type Integrity = 'aligned' | 'off' | 'unknown' // 在位 / 偏离 / 未知

/** 自动化档位轴：账户会不会自己执行（你设的模式，是上下文不是风险）。 */
export type Gate = 'auto' | 'paused' // 自动 / 暂停

/** 在位性判定。 */
export interface IntegrityStatus {
  integrity: Integrity
  /** 风险轴基础判词；模式相关的加重措辞（象限合成）留待渲染层，不在此定死。 */
  text: string
}

/** 档位判定。 */
export interface GateStatus {
  gate: Gate
  /** 模式短标，给账户名旁的安静 chip 用。 */
  label: string
}

/**
 * 从仪表盘项派生**在位性**（风险轴）。
 *
 * 只认持仓 vs 目标（`off_symbol_count`），**刻意不看 `is_started`，也不看 `last_is_success`**。
 * 上次执行成败是尝试质量，不是钱在不在目标上；闭市拒绝下单不能冒充偏离证据。
 *
 * 诚实律：默认「未知」，「在位」要靠证据挣——只有服务端给出 `off_symbol_count===0`
 * 才敢称在位；`>0` 即偏离；缺快照或目标一律未知，不无证推定良好。
 */
export function integrityOf(item: AccountDashboardItem): IntegrityStatus {
  if (item.off_symbol_count == null) {
    if (item.last_exec_at == null) return { integrity: 'unknown', text: '等待首次对账' }
    if (item.asset_observed_at == null) return { integrity: 'unknown', text: '持仓待查询' }
    return { integrity: 'unknown', text: '等待目标与持仓对账' }
  }
  if (item.off_symbol_count === 0) return { integrity: 'aligned', text: '已按策略到位' }
  return { integrity: 'off', text: `${item.off_symbol_count} 只待调整` }
}

/**
 * 从仪表盘项派生**档位**（模式轴）。
 *
 * 纯看 `is_started`。模式天生中性、是上下文——它该落在账户名旁的安静 chip，
 * 绝不占头条把风险（见 :func:`integrityOf`）挤走。
 */
export function gateOf(item: AccountDashboardItem): GateStatus {
  return item.is_started ? { gate: 'auto', label: '自动' } : { gate: 'paused', label: '已暂停' }
}

/** 自动账户偏离目标时需要调仓提醒；主动暂停不因偏离告警。 */
export function needsAdjustmentAttention(item: AccountDashboardItem): boolean {
  return item.is_started && integrityOf(item).integrity === 'off'
}

/** 在位性 × 档位 的四象限判词。 */
export interface StateVerdict {
  integrity: Integrity
  /** 头条主句。 */
  text: string
  /** 是否加重——自动账户敞口偏高时强调。 */
  loud: boolean
}

/** 毛敞口 = Σ|持仓市值| / 总权益（>1 即杠杆）。position_weights 取前 12 大绝对市值，>12 只时略低估。 */
export function grossExposure(item: AccountDashboardItem): number {
  if (!(item.total_asset > 0)) return 0
  const gross = item.position_weights.reduce((sum, w) => sum + Math.abs(w), 0)
  return gross / item.total_asset
}

function lastAttemptFailed(item: AccountDashboardItem): boolean {
  const status = item.last_output_status
  if (status === 'FAILED' || status === 'PARTIAL') return true
  return status !== 'BLOCKED' && item.last_is_success === 0
}

/**
 * 由在位性 × 档位 × 上次尝试性质合成判词与音量.
 *
 * 偏离本身来自仓位差。主动暂停使用中性说明；自动+盘中失败才说「未到位将重试」。
 * 详情 Hero 不展示账户级数量，舰队卡可保留该上下文。
 */
export function stateVerdict(item: AccountDashboardItem, showOffCount = true): StateVerdict {
  const { integrity, text } = integrityOf(item)
  if (integrity === 'unknown' && !item.is_started) return { integrity, text: '已暂停', loud: false }
  if (integrity !== 'off') return { integrity, text, loud: false }
  const countText = showOffCount && item.off_symbol_count != null ? `${item.off_symbol_count} 只待调整 · ` : ''
  if (gateOf(item).gate === 'paused') {
    const pausedText = showOffCount ? `${item.off_symbol_count} 只偏离目标 · 账户已暂停` : '偏离目标 · 账户已暂停'
    return { integrity, text: pausedText, loud: false }
  }
  if (item.last_output_status === 'BLOCKED') {
    const next = item.next_run_time ? formatPlannedAt(item.next_run_time) : null
    return { integrity, text: next ? `${countText}等待下次 ${next}` : `${countText}等待下次自动执行`, loud: false }
  }
  if (lastAttemptFailed(item)) {
    if (grossExposure(item) > 1) return { integrity, text: '上次未到位 · 将自动重试 · 敞口偏高', loud: true }
    return { integrity, text: '上次未到位 · 将自动重试', loud: false }
  }
  if (grossExposure(item) > 1) return { integrity, text: `${countText}将自动重试 · 敞口偏高`, loud: true }
  return { integrity, text: `${countText}将自动重试`, loud: false }
}

/** 敞口条分段颜色：按序渐隐的品牌蓝。 */
export function barColor(i: number): string {
  return `color-mix(in srgb, var(--color-distribution) ${(Math.max(0.22, 0.85 - i * 0.06) * 100).toFixed(0)}%, transparent)`
}

/** 金额格式化：按数量级选精度，保留千分位与等宽。 */
export function formatMoney(v: number): string {
  const abs = Math.abs(v)
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 2 : 4
  return v.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** 持仓一句话描述。 */
export function holdingText(count: number, weights: number[]): string {
  if (count === 0) return '空仓'
  if (weights.length === 0) return `持有 ${count} 只 · 市值不可用`
  const max = weights.length ? Math.max(...weights.map(Math.abs)) : 0
  const total = weights.reduce((s, w) => s + Math.abs(w), 0) || 1
  return `持有 ${count} 只 · 最大 ${((max / total) * 100).toFixed(0)}%`
}

/**
 * 持仓 vs 目标的收敛判定（原子详情用）。
 *
 * `positions` 取自最近执行记录快照，`target` 取自最近目标计算快照。仅当两边都
 * 拿得到时才给结论；否则返回 null（交给上层降级）。
 */
/**
 * 从执行记录中「最近一条带账户资产快照」的记录取持仓列表。
 *
 * 回溯扫描而非只看 `records[0]`：最新一条执行可能是「终止/失败」且不带 `account_assets`
 * 快照，只看最新会把「实际持有仓位」误判为空仓，并据此给出「买入建仓」的错误调仓建议
 * （与后端 dashboard 的 `holdings_count` 相矛盾——后者同样回溯扫描最近若干条取第一条带快照的）。
 * 这里对齐该口径：跳过无快照的记录，取最近一条有快照者；全都没有时返回空数组，交由上层
 * 结合 `holdings_count` 降级为「持仓待刷新」而非坐实「空仓」。
 */
export function positionsOf(records: ExecuteRecord[]): Position[] {
  for (const rec of records) {
    const result = rec.result
    if (result == null || typeof result !== 'object' || Array.isArray(result)) continue
    const assets = result.account_assets
    const positions = assets?.positions
    if (Array.isArray(positions)) return positions
  }
  return []
}

/** 从独立账户资产快照读取持仓；载荷不完整时返回空列表。 */
export function positionsOfAssets(assets: AccountAssets | null | undefined): Position[] {
  return Array.isArray(assets?.positions) ? assets.positions : []
}

/**
 * 当前观测权益：与持仓同一帧的资产快照优先，缺字段时才退回仪表盘聚合。
 *
 * 执行结束后快照已落库。若权益仍读 dashboard 的旧 `total_asset`、持仓读快照，
 * 两块会对不齐。快照里的 0 是有效值（清仓后），不能当缺失。
 */
export function observedTotalAsset(
  snapshotAssets: AccountAssets | null | undefined,
  dashboardTotal: number | null | undefined,
): number {
  const snapshot = snapshotAssets?.total_asset
  if (typeof snapshot === 'number' && Number.isFinite(snapshot)) return snapshot
  if (typeof dashboardTotal === 'number' && Number.isFinite(dashboardTotal)) return dashboardTotal
  return 0
}

/**
 * 无 ``quantities`` 时的权重回退阈值（百分点）。
 * 有服务端目标数量时到位只比手数，不用这把尺子。
 */
export const REBALANCE_THRESHOLD = 0.5
const QTY_EPS = 1e-6

/** 单只调仓动作分类。 */
export type RebalanceAction =
  | 'aligned' // 到位
  | 'increase' // 同向加仓
  | 'reduce' // 同向减仓
  | 'open' // 建仓（从空到有）
  | 'close' // 清仓（到空）
  | 'flip' // 多空翻转

/** 单只调仓行：当前/目标均为「带符号、占权益」的百分数。 */
export interface RebalanceRow {
  symbol: string
  cur: number
  tgt: number
  /** ``cur - tgt``；>0 超配待卖，<0 欠配待买。 */
  delta: number
  amount: number
  side: 'buy' | 'sell' | 'none'
  action: RebalanceAction
}

/** 整账户调仓计划与汇总。 */
export interface RebalancePlan {
  rows: RebalanceRow[]
  /** 待调整（非到位）品种数。 */
  off: number
  buys: number
  sells: number
  flips: number
  /** 当前净敞口 Σcur（%）。 */
  netExposure: number
  /** 当前毛敞口 Σ|cur|（%）。 */
  grossExposure: number
  /** 目标净敞口 Σtgt（%）。 */
  targetNet: number
}

/** 判断持仓方向是否为空头（容错中英文写法）。 */
function isShort(direction: unknown): boolean {
  return typeof direction === 'string' && (direction.includes('空') || direction.toLowerCase().includes('short'))
}

/** 由单只的当前/目标（带符号）派生动作分类。 */
function classifyAction(cur: number, tgt: number, aligned: boolean): RebalanceAction {
  if (aligned) return 'aligned'
  if (Math.abs(cur) < 1e-6) return 'open'
  if (Math.abs(tgt) < 1e-6) return 'close'
  if (Math.sign(cur) !== Math.sign(tgt)) return 'flip'
  return Math.abs(cur) < Math.abs(tgt) ? 'increase' : 'reduce'
}

function signedVolume(position: Position): number | null {
  const extra = position.extra
  if (extra && typeof extra === 'object' && extra !== null && 'net_position' in extra) {
    const net = Number((extra as { net_position?: unknown }).net_position)
    if (Number.isFinite(net)) return net
  }
  if (typeof position.volume !== 'number' || !Number.isFinite(position.volume)) return null
  const mag = Math.abs(position.volume)
  return isShort(position.direction) ? -mag : mag
}

/**
 * 合并当前持仓与目标权重为逐只调仓计划.

 * 与旧实现的本质区别（口径纠正）：当前权重按 ``direction`` 带上多空符号、并以
 * ``equity``（总权益）为分母，与目标（策略净权重）同口径；``delta = cur - tgt``
 * 即真实「要成交多少」，据此判定买/卖方向与动作类别（做空品种的方向不再算反）。

 * Parameters
 * ----------
 * positions : Position[]
 *     当前持仓（``market_value`` 为幅度，``direction`` 表方向）。
 * target : LatestWeights
 *     目标权重（分数，可为负=做空）。
 * equity : number
 *     账户总权益，作为归一分母；``<= 0`` 时当前权重按 0 处理。
 * quantities : LatestWeights | null | undefined
 *     服务端量化后的目标数量；缺省时回退权重阈值，不在前端写渠道公式。

 * Returns
 * -------
 * RebalancePlan
 *     逐只行（按 ``|delta|`` 降序）与账户级汇总。
 */
export function rebalancePlan(
  positions: Position[],
  target: LatestWeights,
  equity: number,
  quantities?: LatestWeights | null,
): RebalancePlan {
  const curMv = new Map<string, number>()
  const curQty = new Map<string, number>()
  const notional = new Map<string, number>()
  for (const p of positions) {
    if (typeof p.symbol !== 'string') continue
    const mag = Math.abs(Number(p.market_value) || 0)
    const signed = isShort(p.direction) ? -mag : mag
    curMv.set(p.symbol, (curMv.get(p.symbol) ?? 0) + signed)
    const lots = signedVolume(p)
    if (lots != null) curQty.set(p.symbol, (curQty.get(p.symbol) ?? 0) + lots)
    const volume = Math.abs(Number(p.volume) || 0)
    if (volume > 0 && mag > 0) notional.set(p.symbol, mag / volume)
  }
  const base = equity > 0 ? equity : 0
  const useQty = quantities != null
  const syms = new Set<string>([
    ...curMv.keys(),
    ...Object.keys(target),
    ...(useQty ? Object.keys(quantities) : []),
  ])
  const rows: RebalanceRow[] = []
  for (const s of syms) {
    const cur = base > 0 ? ((curMv.get(s) ?? 0) / base) * 100 : 0
    const tgt = (target[s] ?? 0) * 100
    if (Math.abs(cur) < 1e-6 && Math.abs(tgt) < 1e-6 && !(useQty && s in quantities)) continue
    let aligned: boolean
    let amount: number
    if (useQty) {
      const currentLots = curQty.get(s) ?? 0
      const knownLots = curQty.has(s)
      if (s in quantities) {
        const targetLots = quantities[s] ?? 0
        aligned = knownLots || Math.abs(cur) < 1e-6
          ? Math.abs(currentLots - targetLots) <= QTY_EPS
          : false
        const unit = notional.get(s)
        amount = unit != null && base > 0
          ? (Math.abs(currentLots - targetLots) * unit / base) * 100
          : Math.abs(cur - tgt)
      } else {
        aligned = Math.abs(currentLots) <= QTY_EPS && Math.abs(tgt) < 1e-6
        amount = Math.abs(cur - tgt)
      }
    } else {
      const delta = +(cur - tgt).toFixed(2)
      aligned = Math.abs(delta) <= REBALANCE_THRESHOLD
      amount = Math.abs(delta)
    }
    const delta = +(cur - tgt).toFixed(2)
    const action = classifyAction(cur, tgt, aligned)
    const side = action === 'aligned' ? 'none' : delta < 0 ? 'buy' : 'sell'
    rows.push({ symbol: s, cur, tgt, delta, amount, side, action })
  }
  rows.sort((a, b) => b.amount - a.amount)
  return {
    rows,
    off: rows.filter((r) => r.action !== 'aligned').length,
    buys: rows.filter((r) => r.side === 'buy').length,
    sells: rows.filter((r) => r.side === 'sell').length,
    flips: rows.filter((r) => r.action === 'flip').length,
    netExposure: rows.reduce((sum, r) => sum + r.cur, 0),
    grossExposure: rows.reduce((sum, r) => sum + Math.abs(r.cur), 0),
    targetNet: rows.reduce((sum, r) => sum + r.tgt, 0),
  }
}

/** 将服务端唯一真源的可执行计划适配为详情图表使用的百分比行。 */
export function rebalancePlanOfServer(plan: AccountRebalancePlan | null): RebalancePlan {
  const rows: RebalanceRow[] = (plan?.rows ?? []).map((row) => {
    const cur = row.current_weight * 100
    const tgt = row.target_weight * 100
    return {
      symbol: row.symbol,
      cur,
      tgt,
      delta: +(cur - tgt).toFixed(2),
      amount: Math.abs(cur - tgt),
      side: row.side,
      action: row.action,
    }
  })
  return {
    rows,
    off: plan?.off_symbol_count ?? 0,
    buys: rows.filter((row) => row.side === 'buy').length,
    sells: rows.filter((row) => row.side === 'sell').length,
    flips: rows.filter((row) => row.action === 'flip').length,
    netExposure: rows.reduce((sum, row) => sum + row.cur, 0),
    grossExposure: rows.reduce((sum, row) => sum + Math.abs(row.cur), 0),
    targetNet: rows.reduce((sum, row) => sum + row.tgt, 0),
  }
}

/**
 * 账户「是否调仓到位」的一句话汇总（供仪表盘卡片用）.

 * 与持仓明细抽屉共用 :func:`rebalancePlan` 的幅度口径，两处数字始终一致。
 */
export function convergence(
  positions: Position[],
  target: LatestWeights,
  equity: number,
): { level: StatusLevel; text: string; offCount: number } {
  const plan = rebalancePlan(positions, target, equity)
  if (plan.rows.length === 0) return { level: 'ok', text: '空仓 · 与目标一致', offCount: 0 }
  if (plan.off === 0) return { level: 'ok', text: '已调仓到位', offCount: 0 }
  return { level: 'warn', text: `${plan.off} 个品种与目标背离`, offCount: plan.off }
}
