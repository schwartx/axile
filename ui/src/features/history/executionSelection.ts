import { executionOutcome } from '@/features/account/executionOutcome'
import type { AnalysisExecutionSummary, CostExecutionRow } from '@/lib/api/performance'
import type { ExecutionCursor } from '@/features/history/executionMarkers'
import type { ChartSelection } from '@/features/history/chartModel'
import { shanghaiTime } from '@/features/history/costs'

export function executionSelection(row: CostExecutionRow | AnalysisExecutionSummary): Extract<ChartSelection, { kind: 'execution' }> {
  return 'record_id' in row
    ? { kind: 'execution', recordId: row.record_id, time: row.time_ms }
    : { kind: 'execution', recordId: row.record.id, time: shanghaiTime(row.record.created_at) }
}

export function executionState(row: CostExecutionRow): string {
  return executionOutcome(row.record.raw_result).text
}

export function hoverExecution(rows: AnalysisExecutionSummary[], cursor: ExecutionCursor | null): AnalysisExecutionSummary | null {
  if (!cursor) return null
  if (cursor.recordId != null) return rows.find(row => row.record_id === cursor.recordId) ?? null
  let latest: AnalysisExecutionSummary | null = null
  for (const row of rows) {
    if (row.time_ms <= cursor.time && (!latest || row.time_ms > latest.time_ms || row.time_ms === latest.time_ms && row.record_id > latest.record_id)) latest = row
  }
  return latest
}

export function executionAt(rows: AnalysisExecutionSummary[], time: number): AnalysisExecutionSummary | null {
  let low = 0, high = rows.length
  while (low < high) {
    const mid = (low + high) >>> 1
    if (rows[mid].time_ms <= time) low = mid + 1
    else high = mid
  }
  return rows[low - 1] ?? null
}
