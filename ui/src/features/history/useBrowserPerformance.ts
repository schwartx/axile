import { usePerformanceSnapshot } from '@/features/history/usePerformanceSnapshot'
import { shanghaiDay } from '@/features/history/costs'
import type { PerformanceSummary } from '@/types/api'

export function useBrowserPerformance(accountId: number, observedAt?: string | null) {
  const { metadata, displayed, checking, statusError, resultError } = usePerformanceSnapshot(accountId, 'all')
  const points = displayed?.result.points ?? []
  const last = points.at(-1)
  const day = observedAt ? shanghaiDay(observedAt) : null
  const previousClose = day ? points.findLast(point => point.date < day)?.account_equity ?? null : null
  const error = statusError ?? resultError
  const summary: PerformanceSummary = {
    snapshot_id: displayed?.snapshot_id ?? null,
    status: error ? 'failed' : metadata?.status ?? (checking ? 'pending' : 'empty'),
    computed_at: displayed?.computed_at ?? null,
    observed_at: last?.observed_at ?? last?.date ?? null,
    account_equity: last?.account_equity ?? null,
    account_daily_return: last?.account_daily_return ?? null,
    points,
  }
  return { summary, previousClose, error: error?.message ?? metadata?.error ?? null }
}
