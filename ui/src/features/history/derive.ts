/**
 * 回看/绩效页的数据派生。
 *
 * 全部基于真实执行记录与绑定记录：
 * - 权益序列取各成功记录 `raw_result.account_assets.total_asset`。
 * - 分段按 `portfolio_records` 的绑定边界切。
 *
 * 口径说明（估值 vs 真盈亏）
 * --------------------------
 * 系统无入金/出金数据源，权益差既可能是真盈亏、也可能是资金搬家。故：
 * - 展示层对**孤立退化快照**（尖刺/深坑且随即回弹）做离群剔除，避免坏读数砸曲线。
 * - 用 `detectTransfers` 启发式标出**疑似资金进出**（相邻步权益跳变过大），
 *   仅**标注不剔除**（无真源不伪造数）；据此把「区间盈亏」的免责条件化。
 */
import type { EquityPoint } from '@/components/viz/EquityChart'
import type { ScheduleSkipActivity } from '@/lib/api/accounts'
import type { AccountAssetSnapshot, ExecuteRecord, PortfolioAccountRecord } from '@/types/api'
import { executionOutcome } from '@/features/account/executionOutcome'

export type RangeKey = '30' | '90' | 'all'

/** 相邻步权益相对跳变超过该比例即记为「疑似资金进出」。 */
const TRANSFER_JUMP_RATIO = 0.25
/** 孤立点相对左右两邻居偏离超过该比例且同向即判为退化快照，剔除。 */
const OUTLIER_DEV_RATIO = 0.4

/** 从账户资产快照取总权益（容错）。 */
function totalAsset(snapshot: AccountAssetSnapshot): number | null {
  const v = snapshot.assets?.total_asset
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : null
}

/** 判断记录是否空跑（目标未变）。 */
function isNoop(r: ExecuteRecord): boolean {
  const curr = r.raw_input?.curr_target ?? {}
  const last = r.raw_input?.last_target ?? {}
  const keys = new Set([...Object.keys(curr), ...Object.keys(last)])
  for (const k of keys) if (Math.abs((curr[k] ?? 0) - (last[k] ?? 0)) > 1e-9) return false
  return true
}

/** 记录按时间升序，并按区间过滤（相对最后一条记录日期回溯 N 天）。 */
function filterCreatedAtRange<T extends { created_at: string }>(items: T[], range: RangeKey): T[] {
  const asc = [...items].sort((a, b) => a.created_at.localeCompare(b.created_at))
  if (range === 'all' || asc.length === 0) return asc
  const days = Number(range)
  const last = new Date(asc[asc.length - 1].created_at).getTime()
  const cutoff = last - days * 864e5
  return asc.filter((r) => new Date(r.created_at).getTime() >= cutoff)
}

export function filterRecords(records: ExecuteRecord[], range: RangeKey): ExecuteRecord[] {
  return filterCreatedAtRange(records, range)
}

export function filterAssetSnapshots(
  snapshots: AccountAssetSnapshot[],
  range: RangeKey,
): AccountAssetSnapshot[] {
  return filterCreatedAtRange(snapshots, range)
}

/**
 * 判断下标 `i` 是否为孤立退化快照（尖刺/深坑且随即回弹）。
 *
 * 仅当中点相对**左右两邻居**同向偏离超过阈值才判离群——单侧永久台阶
 * （疑似资金进出）不会被误剔，交由 `detectTransfers` 标注。
 */
function isOutlier(eqs: number[], i: number): boolean {
  if (i <= 0 || i >= eqs.length - 1) return false
  const left = eqs[i - 1]
  const mid = eqs[i]
  const right = eqs[i + 1]
  const pit = mid < left * (1 - OUTLIER_DEV_RATIO) && mid < right * (1 - OUTLIER_DEV_RATIO)
  const spike = mid > left * (1 + OUTLIER_DEV_RATIO) && mid > right * (1 + OUTLIER_DEV_RATIO)
  return pit || spike
}

/** 账户资产观测 → 权益曲线点（含原始 ISO 时间戳），并剔除孤立退化快照。 */
export function buildEquityPoints(snapshots: AccountAssetSnapshot[]): EquityPoint[] {
  const raw: EquityPoint[] = []
  for (const snapshot of snapshots) {
    const eq = totalAsset(snapshot)
    if (eq == null) continue
    raw.push({
      date: snapshot.created_at.replace('T', ' ').slice(5, 16),
      iso: snapshot.created_at,
      eq,
    })
  }
  const eqs = raw.map((p) => p.eq)
  return raw.filter((_, i) => !isOutlier(eqs, i))
}

/** 疑似资金进出标记：`index` 对应权益点下标，`amount` 为该步权益跳变额（带符号）。 */
export interface TransferMark {
  index: number
  amount: number
}

/**
 * 启发式检测疑似资金进出。
 *
 * 相邻两点权益相对跳变超过 `TRANSFER_JUMP_RATIO` 即记一笔——无净转入真源，
 * 只标不减：用于把「区间盈亏」免责条件化、并在曲线上做中性竖标。
 */
export function detectTransfers(points: EquityPoint[]): TransferMark[] {
  const marks: TransferMark[] = []
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1].eq
    const curr = points[i].eq
    if (prev > 0 && Math.abs(curr - prev) / prev > TRANSFER_JUMP_RATIO) {
      marks.push({ index: i, amount: curr - prev })
    }
  }
  return marks
}

export interface HistoryStats {
  fills: number
  noops: number
  fails: number
  /** 被终止的执行次数（graceful/cancel）；不计入 fails，语义是「提前收尾」而非「失败」。 */
  terminated: number
  currency: string
  pnl: number | null
  eqFirst: number | null
  eqLast: number | null
  /** 区间内疑似资金进出（用于条件化免责与曲线标注）；空则视为纯真盈亏。 */
  transfers: TransferMark[]
}

/** 汇总区间内的执行统计与盈亏。 */
export function aggregateStats(
  records: ExecuteRecord[],
  points: EquityPoint[],
  snapshotCurrency = '',
): HistoryStats {
  let fills = 0
  let noops = 0
  let fails = 0
  let terminated = 0
  let currency = ''
  for (const r of records) {
    if (r.raw_result?.task_status === 'TERMINATED') {
      // 终止 = 人工/系统提前收尾，非硬失败，单列不计入 fails（否则「失败 N 次」被终止灌水）。
      terminated++
    } else if (r.is_success === 1) {
      fills++
      // 成功清仓确有平仓成交，不按调仓口径算「空跑」（否则清仓被误当没发生）。
      if (r.raw_result?.execution_kind !== 'clear_positions' && isNoop(r)) noops++
    } else if (!executionOutcome(r).sessionScope) {
      fails++
    }
    const c = r.raw_result?.account_assets?.currency
    if (typeof c === 'string' && c) currency = c
  }
  const eqFirst = points.length ? points[0].eq : null
  const eqLast = points.length ? points[points.length - 1].eq : null
  const pnl = eqFirst != null && eqLast != null ? eqLast - eqFirst : null
  return {
    fills,
    noops,
    fails,
    terminated,
    currency: currency || snapshotCurrency,
    pnl,
    eqFirst,
    eqLast,
    transfers: detectTransfers(points),
  }
}

export interface Segment {
  portfolioId: number | null
  start: string
  pnl: number | null
}

/**
 * 按绑定边界切分收益段。
 *
 * `bindings` 升序；每段的盈亏 = 段末权益 − 段初权益（用区间内的权益点估算）。
 */
export function buildSegments(
  bindings: PortfolioAccountRecord[],
  points: EquityPoint[],
): Segment[] {
  if (points.length === 0) return []
  const asc = [...bindings].sort((a, b) => a.created_at.localeCompare(b.created_at))
  // 仅保留有绑定组合的边界；无绑定期不计段。
  const bound = asc.filter((b) => b.portfolio_id != null)
  if (bound.length === 0) {
    return [{ portfolioId: null, start: points[0].date, pnl: points[points.length - 1].eq - points[0].eq }]
  }
  const segs: Segment[] = []
  for (let i = 0; i < bound.length; i++) {
    const startTs = new Date(bound[i].created_at).getTime()
    const endTs = i + 1 < bound.length ? new Date(bound[i + 1].created_at).getTime() : Infinity
    const seg = points.filter((p) => {
      const t = new Date(p.iso).getTime()
      return t >= startTs && t < endTs
    })
    const pnl = seg.length >= 2 ? seg[seg.length - 1].eq - seg[0].eq : null
    segs.push({
      portfolioId: bound[i].portfolio_id,
      start: bound[i].created_at.replace('T', ' ').slice(5, 16),
      pnl,
    })
  }
  return segs
}

/** 单根每日增量柱。 */
export interface DailyBar {
  /** 展示用日标签（MM-DD）。 */
  day: string
  /** 当日盈亏 = 当日末权益 − 上一活跃日末权益（首个活跃日取当日内 末 − 初）。 */
  delta: number
  /** 当日末权益（收盘估值），供 hover 读数显示权益水平。 */
  endEq: number
  /** `trade`=交易盈亏（红绿）；`transfer`=当日含疑似资金进出（中性，不算盈亏）。 */
  kind: 'trade' | 'transfer'
  /** 距上一活跃日的日历天数（首日为 0）；> 3 天视为存在数据真空。 */
  gapDaysBefore: number
}

/** ISO 时间戳的日历日键（YYYY-MM-DD）。 */
function dayKey(iso: string): string {
  return iso.slice(0, 10)
}

/**
 * 权益点 → 每日增量柱。
 *
 * 按日历日分组，每日取首/末权益点：首个活跃日的当日盈亏取「当日末 − 当日初」
 * （无前一日参照），其余取「当日末 − 上一活跃日末」，故各柱之和≈区间总盈亏。
 * 命中疑似资金进出的日子标 `transfer`（中性），避免把资金搬家画成交易盈利。
 */
export function buildDailyBars(points: EquityPoint[], transfers: TransferMark[] = []): DailyBar[] {
  if (points.length === 0) return []
  // points 已升序：按日聚合，保留每日首点与末点。
  const firstOfDay = new Map<string, EquityPoint>()
  const lastOfDay = new Map<string, EquityPoint>()
  for (const p of points) {
    const k = dayKey(p.iso)
    if (!firstOfDay.has(k)) firstOfDay.set(k, p)
    lastOfDay.set(k, p)
  }
  const transferDays = new Set(transfers.map((t) => dayKey(points[t.index].iso)))
  const days = [...lastOfDay.keys()].sort()

  const bars: DailyBar[] = []
  let prevLastEq: number | null = null
  let prevDay: string | null = null
  for (const d of days) {
    const dayLast = lastOfDay.get(d)!.eq
    const dayFirst = firstOfDay.get(d)!.eq
    const delta = prevLastEq == null ? dayLast - dayFirst : dayLast - prevLastEq
    const gap = prevDay ? Math.round((new Date(d).getTime() - new Date(prevDay).getTime()) / 864e5) : 0
    bars.push({
      day: d.slice(5),
      delta,
      endEq: dayLast,
      kind: transferDays.has(d) ? 'transfer' : 'trade',
      gapDaysBefore: gap,
    })
    prevLastEq = dayLast
    prevDay = d
  }
  return bars
}

export interface HistoryEvent {
  date: string
  kind: 'create' | 'rebind' | 'fail' | 'skip'
  tag: string
  text: string
  /** 失败事件对应的执行 id，可点开执行详情抽屉；折叠汇总行为 null。 */
  executionId?: string | null
}

/** 逐条展开失败事件的上限，超出部分折叠成一条汇总提示，避免刷屏。 */
const FAIL_EVENT_CAP = 8

/** 从执行记录里取顶层失败原因（后端写在 raw_result.error）。 */
/** 从绑定记录 + 失败执行构建账户时间线。 */
export function buildEvents(
  bindings: PortfolioAccountRecord[],
  records: ExecuteRecord[],
  skips: ScheduleSkipActivity[] = [],
): HistoryEvent[] {
  const events: HistoryEvent[] = []
  const asc = [...bindings].sort((a, b) => a.created_at.localeCompare(b.created_at))
  asc.forEach((b, i) => {
    const date = b.created_at.replace('T', ' ').slice(5, 16)
    if (i === 0) {
      events.push({ date, kind: 'create', tag: '创建', text: b.portfolio_id != null ? `账户创建 · 绑定组合 #${b.portfolio_id}` : '账户创建 · 未绑定组合' })
    } else {
      events.push({ date, kind: 'rebind', tag: '换绑', text: b.portfolio_id != null ? `换绑组合 → #${b.portfolio_id}` : '解绑组合' })
    }
  })

  // 失败逐条展开、每条可点开执行详情；超过上限的更早失败折叠成一条汇总。
  // 终止（task_status=TERMINATED）不是失败，排除在外，避免时间线把「提前收尾」渲染成「执行失败」。
  const failRecords = records
    .filter((r) => executionOutcome(r.raw_result).state === 'FAILED')
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
  for (const r of failRecords.slice(0, FAIL_EVENT_CAP)) {
    const view = executionOutcome(r.raw_result)
    events.push({
      date: r.created_at.replace('T', ' ').slice(5, 16),
      kind: 'fail',
      tag: '失败',
      text: view.reason ? `${view.text} · ${view.reason}` : view.text,
      executionId: r.execution_id,
    })
  }
  if (failRecords.length > FAIL_EVENT_CAP) {
    const oldest = failRecords[failRecords.length - 1]
    events.push({
      date: oldest.created_at.replace('T', ' ').slice(5, 16),
      kind: 'fail',
      tag: '失败',
      text: `更早还有 ${failRecords.length - FAIL_EVENT_CAP} 次失败（已折叠）`,
      executionId: null,
    })
  }

  const activity = [
    ...records.map((record) => ({ type: 'record' as const, time: record.created_at })),
    ...skips.map((skip) => ({ type: 'skip' as const, time: skip.occurred_at })),
  ].sort((a, b) => b.time.localeCompare(a.time))
  let index = 0
  while (index < activity.length) {
    const current = activity[index]
    if (current.type !== 'skip') {
      index++
      continue
    }
    let end = index + 1
    while (end < activity.length && activity[end].type === 'skip') end++
    const count = end - index
    events.push({
      date: current.time.replace('T', ' ').slice(5, 16),
      kind: 'skip',
      tag: '跳过',
      text: count > 1 ? `连续 ${count} 次排程因休市跳过` : '排程已跳过 · 当日休市',
      executionId: null,
    })
    index = end
  }

  return events.sort((a, b) => b.date.localeCompare(a.date))
}

/** 按回看区间过滤休市跳过记录，不影响执行记录的收益口径。 */
export function filterScheduleSkips(
  skips: ScheduleSkipActivity[],
  records: ExecuteRecord[],
  range: RangeKey,
): ScheduleSkipActivity[] {
  if (range === 'all' || skips.length === 0) return [...skips]
  const latest = Math.max(
    ...records.map((record) => new Date(record.created_at).getTime()),
    ...skips.map((skip) => new Date(skip.occurred_at).getTime()),
  )
  const cutoff = latest - Number(range) * 864e5
  return skips.filter((skip) => new Date(skip.occurred_at).getTime() >= cutoff)
}
