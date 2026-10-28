import { getAccountActivity, type AccountActivity, type AccountActivityList } from '@/lib/api/accounts'
import { shanghaiTime } from '@/features/account/executionValues'
import { costTrades, type CostTrade } from '@/features/history/costs'
import type { ExecuteRecord } from '@/types/api'

export interface TimeWindow { start: number; end: number }

/** 把毫秒窗编成活动流 since/until（上海 naive，与 created_at 同形）。 */
export function activityWindowQuery(window: TimeWindow): { since: string; until: string } {
  const iso = (ms: number) => new Date(ms + 8 * 3600000).toISOString().slice(0, 19)
  return { since: iso(window.start), until: iso(window.end) }
}

export interface LiveWindow { since: string; until: string }

/** 从冻结结果事实抽出成交。 */
export function tradesFromResult(result: Record<string, unknown> | null,
  record: { id?: number | null; execution_id?: string | null; created_at: string }): CostTrade[] {
  if (!result) return []
  return costTrades({ id: record.id ?? null, execution_id: record.execution_id ?? null,
    created_at: record.created_at, result: result as ExecuteRecord['result'] })
}

/** 统一活动流按时间倒序分页；读到区间下界才停止，失败不返回半份汇总。 */
export async function loadJournal(
  accountId: number,
  window: TimeWindow,
  signal: AbortSignal,
  fetchPage: (skip: number) => Promise<AccountActivityList> = (skip) => getAccountActivity(accountId, { skip, limit: 500, ...activityWindowQuery(window) }, signal),
  parseTime: (value: string) => number = shanghaiTime,
): Promise<AccountActivity[]> {
  const result: AccountActivity[] = []
  const seen = new Set<string>()
  let count: number | null = null
  for (let skip = 0; ; ) {
    signal.throwIfAborted()
    const page = await fetchPage(skip)
    signal.throwIfAborted()
    if (count != null && count !== page.count) throw new Error('执行记录在加载期间发生变化，请刷新重试')
    count = page.count
    if (!page.data.length && skip < count) throw new Error('执行记录分页不完整，请重试')
    for (const activity of page.data) {
      const key = activity.kind === 'execution' ? `execution:${activity.record.id ?? activity.record.execution_id}` : `skip:${activity.id}`
      if (seen.has(key)) throw new Error('执行记录分页发生重叠，请刷新重试')
      seen.add(key)
      const time = parseTime(activity.occurred_at)
      if (!Number.isFinite(time)) throw new Error('执行记录时间无效，无法确认统计范围')
      if (time >= window.start && time < window.end) result.push(activity)
    }
    skip += page.data.length
    if (skip >= count || page.data.some((a) => parseTime(a.occurred_at) < window.start)) return result
  }
}

