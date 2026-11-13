import type { PerformanceSummary } from '@/types/api'
import { shanghaiDay } from '@/features/history/costs'
import { performanceViews, performanceViewports } from '@/features/history/viewState'
import { checkPerformance } from '@/features/history/performanceCache'

export function cardPerformance(
  summary?: PerformanceSummary,
  now = Date.now(),
  live?: { equity: number | null; observedAt?: string | null; previousClose?: number | null; adjustedReturn?: number | null },
) {
  const liveDay = live?.observedAt ? shanghaiDay(live.observedAt) : null
  const day = liveDay ?? (summary?.observed_at ? shanghaiDay(summary.observed_at) : null)
  const today = shanghaiDay(new Date(now).toISOString())
  const livePct =
    live?.equity != null && live.previousClose != null && live.previousClose > 0
      ? (live.equity / live.previousClose - 1) * 100
      : null
  return {
    equity: summary?.account_equity ?? null,
    pct: live?.adjustedReturn !== undefined ? (live.adjustedReturn == null ? null : live.adjustedReturn * 100) : livePct ?? (summary?.account_daily_return == null ? null : summary.account_daily_return * 100),
    dayLabel: day == null ? '日涨跌' : day === today ? '今日' : day.replace(
      /^(\d+)-0?(\d+)-0?(\d+)$/,
      (_, year, month, date) => `${year === today.slice(0, 4) ? '' : `${year}年`}${month}月${date}日`,
    ),
  }
}

export function openFullPerformance(accountId: number) {
  performanceViews.set(accountId, { range: 'all', view: 'cumulative', scale: 'observations', selection: null })
  performanceViewports.delete(`${accountId}:all`)
  void checkPerformance(accountId, 'all')
}

/** 当前金额只使用真实资产观测，历史绩效独立展示。 */
export function currentEquity(item: { total_asset: number; asset_observed_at?: string | null }) {
  return item.asset_observed_at ? item.total_asset : null
}
