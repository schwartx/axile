import { executionOutcome } from '@/features/account/executionOutcome'
import type { AnalysisExecutionSummary, CostExecutionRow } from '@/lib/api/performance'
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
