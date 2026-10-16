import { executionOutcome, symbolPreview } from '@/features/account/executionOutcome'
/**
 * 「近期执行」的折叠/限量逻辑（纯函数，可测）。
 *
 * 原子视图的近期执行是「一瞥」，不是完整日志（那是 /history）。故：
 * - 连续的空跑（noop）折叠成一行；
 * - 连续的硬失败折叠成「连续 N 次执行失败」；
 * - 连续的部分成功自折，文案两面说（到位 + 未成），不与硬失败混折；
 * - 成交（fill）逐条保留；
 * - 整体限量到 cap 行，其余交给「完整回看」。
 */
import { formatMoney } from '@/lib/derive'
import type { AccountActivity, ActivityExecutionRecord } from '@/lib/api/accounts'
import { executionReasonText } from '@/features/account/executionReason'

type Kind = 'fill' | 'clear' | 'noop' | 'fail' | 'partial' | 'terminated' | 'blocked' | 'legacy' | 'unknown'

export type RecentRow =
  | { type: 'legacy' | 'unknown'; key: string; time: string; executionId: string | null }
  | { type: 'fill'; key: string; time: string; executionId: string | null; desc: string; amount: string }
  | { type: 'noop'; key: string; time: string; count: number; clear?: boolean }
  | { type: 'fail'; key: string; time: string; count: number; saturated: boolean; executionId: string | null; reason: string }
  | {
      type: 'partial'
      clear?: boolean
      key: string
      time: string
      count: number
      saturated: boolean
      executionId: string | null
      reached: number | null
      failed: number | null
      reason: string
      amount: string
    }
  | { type: 'blocked'; key: string; time: string; count: number; executionId: string | null; reason: string; sessionScope?: 'all' | null }
  | { type: 'terminated'; key: string; time: string; count: number; executionId: string | null }
  | { type: 'skip'; key: string; time: string; count: number; reason: string }

function assetAmount(r: ActivityExecutionRecord): string {
  return typeof r.total_asset === 'number' ? formatMoney(r.total_asset) : ''
}

/**
 * 记录分类：终止 / 约束跳过 / 部分成功 / 失败 / 清仓 / 空跑 / 成交。
 *
 * 先判 `task_status==='TERMINATED'`：终止是「人工/系统提前收尾」，非硬失败。
 * 再判执行器 `BLOCKED`：非交易时段等约束，不进「连续失败」。
 * 再判部分成功：有品种到位、有品种未成（或 `status==='PARTIAL'`），不进硬失败。
 * 再判成功清仓（`execution_kind==='clear_positions'`）：确有平仓成交，不应按调仓口径误判为「空跑」。
 */
function kindOf(r: ActivityExecutionRecord): Kind {
  const { state, sessionScope } = executionOutcome(r)
  if (sessionScope === 'partial') return 'partial'
  if (state === 'UNKNOWN') return 'unknown'
  if (state === 'BLOCKED') return 'blocked'
  if (state === 'TERMINATED') return 'terminated'
  if (state === 'FAILED') return 'fail'
  if (state === 'PARTIAL') return 'partial'
  if (state === 'NOOP') return 'noop'
  return r.execution_kind === 'clear_positions' ? 'clear' : 'fill'
}

/** 成交/清仓行的描述与金额（两者都是「有成交的成功执行」，同一 ✓ 样式）。 */
function fillRow(r: ActivityExecutionRecord, i: number, kind: 'fill' | 'clear'): RecentRow {
  const amount = assetAmount(r)
  return {
    type: 'fill',
    key: `${kind === 'clear' ? 'c' : 'f'}${r.id ?? r.execution_id ?? i}`,
    time: r.created_at,
    executionId: r.execution_id ?? null,
    desc: executionOutcome(r, kind === 'clear').text,
    amount: amount || '—',
  }
}

function partialRow(latest: ActivityExecutionRecord, i: number, count: number, saturated: boolean): RecentRow {
  const view = executionOutcome(latest)
  return {
    type: 'partial',
    clear: latest.execution_kind === 'clear_positions',
    key: `p${i}`,
    time: latest.created_at,
    count,
    saturated,
    executionId: latest.execution_id ?? null,
    reached: null,
    failed: view.symbols.length || null,
    reason: view.sessionScope === 'partial' ? `部分品种休市，${view.symbols.length} 个品种未处理` : symbolPreview(view.symbols),
    amount: assetAmount(latest),
  }
}

/** 一瞥行主文案；详情与执行记录页共用，避免两处各写一句。 */
export function recentRowText(row: RecentRow): string {
  if (row.type === 'legacy' || row.type === 'unknown') return '执行状态未知'
  if (row.type === 'fill') return row.desc
  if (row.type === 'noop') {
    const title = row.clear ? '无需清仓' : '无需调仓'
    return row.count > 1 ? `连续 ${row.count} 次${title}` : title
  }
  if (row.type === 'fail') {
    const head = row.count > 1 ? `连续 ${row.count}${row.saturated ? '+' : ''} 次执行失败` : '执行失败'
    return row.reason ? `${head} · 最近：${row.reason}` : head
  }
  if (row.type === 'partial') {
    const title = executionOutcome({ status: 'PARTIAL' }).title
    const head = row.count > 1 ? `连续 ${row.count}${row.saturated ? '+' : ''} 次${title}` : title
    return row.reason ? `${head} · ${row.reason}` : head
  }
  if (row.type === 'terminated') return row.count > 1 ? `执行已终止 · ${row.count} 次` : '执行已终止'
  if (row.type === 'skip') return row.count > 1 ? `连续 ${row.count} 次${row.reason}` : row.reason
  if (row.type !== 'blocked') return ''
  if (row.sessionScope === 'all') return row.count > 1 ? `连续 ${row.count} 次休市跳过` : '休市跳过'
  return `${row.count > 1 ? `${row.count} 次未执行` : '未执行'}${row.reason ? ` · ${row.reason}` : ''}`
}

export interface RecentResult {
  rows: RecentRow[]
  /** 折叠/限量后仍有更多，去完整回看看。 */
  truncated: boolean
}

/**
 * 把（按时间倒序的）执行记录折叠成近期执行行。
 *
 * Parameters
 * ----------
 * records : 按 created_at 倒序（最新在前）的执行记录。
 * cap : 最多展示的行数（成交行与折叠组各算一行）。
 * fetchLimit : 拉取时用的 limit；用于判断末尾的失败/空跑组是否「饱和」（窗口拉满、
 *              库里可能还有更多同类，展示为 N+）。
 */
/** 把统一账户活动流折叠为近期展示行。 */
export function buildRecentActivity(
  activity: AccountActivity[],
  opts: { cap?: number; fetchLimit?: number } = {},
): RecentResult {
  const cap = opts.cap ?? 6
  const fetchLimit = opts.fetchLimit ?? activity.length
  const windowFull = activity.length >= fetchLimit

  const all: RecentRow[] = []
  let i = 0
  while (i < activity.length) {
    const current = activity[i]
    if (current.kind === 'schedule_skip') {
      let j = i + 1
      while (j < activity.length && activity[j].kind === 'schedule_skip') j += 1
      all.push({
        type: 'skip',
        key: `s${current.id}`,
        time: current.occurred_at,
        count: j - i,
        reason: executionReasonText(current.reason_code, '排程已跳过'),
      })
      i = j
      continue
    }
    const k = kindOf(current.record)
    if (k === 'legacy' || k === 'unknown') {
      all.push({ type: k, key: `${k}${current.record.id ?? i}`, time: current.record.created_at, executionId: current.record.execution_id ?? null })
      i += 1
      continue
    }
    if (k === 'fill' || k === 'clear') {
      all.push(fillRow(current.record, i, k))
      i += 1
      continue
    }
    // 折叠一段连续的同类（noop / fail / partial / terminated / blocked）
    let j = i
    while (j < activity.length) {
      const candidate = activity[j]
      if (candidate.kind !== 'execution' || kindOf(candidate.record) !== k || candidate.record.execution_kind !== current.record.execution_kind || executionOutcome(candidate.record).sessionScope !== executionOutcome(current.record).sessionScope) break
      j += 1
    }
    const run = activity.slice(i, j).map((item) => item.kind === 'execution' ? item.record : null).filter((item): item is ActivityExecutionRecord => item != null)
    const latest = run[0]
    const saturated = j === activity.length && windowFull
    if (k === 'noop') {
      all.push({ type: 'noop', key: `n${i}`, time: latest.created_at, count: run.length, clear: latest.execution_kind === 'clear_positions' })
    } else if (k === 'terminated') {
      all.push({
        type: 'terminated',
        key: `t${i}`,
        time: latest.created_at,
        count: run.length,
        executionId: latest.execution_id ?? null,
      })
    } else if (k === 'blocked') {
      const view = executionOutcome(latest)
      all.push({
        type: 'blocked',
        key: `b${i}`,
        time: latest.created_at,
        count: run.length,
        executionId: latest.execution_id ?? null,
        sessionScope: view.sessionScope === 'all' ? 'all' : null,
        reason: view.title.startsWith('未执行 · ') ? view.title.slice('未执行 · '.length) : '',
      })
    } else if (k === 'partial') {
      all.push(partialRow(latest, i, run.length, saturated))
    } else {
      all.push({
        type: 'fail',
        key: `x${i}`,
        time: latest.created_at,
        count: run.length,
        saturated,
        executionId: latest.execution_id ?? null,
        reason: executionOutcome(latest).reason,
      })
    }
    i = j
  }

  return { rows: all.slice(0, cap), truncated: all.length > cap }
}
