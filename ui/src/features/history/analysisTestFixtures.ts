import type { AnalysisChartResponse, AnalysisMetadata, AnalysisTable, PerformanceRange } from '@/lib/api/performance'

export const summaryColumns = ['value', 'cost', 'loss_bp', 'coverage', 'covered', 'count', 'amount_complete', 'fee_covered', 'covered_value', 'estimated']
export const indexColumns = ['record_id', 'execution_id', 'time', 'created_at', 'is_success', 'noop', 'has_transactions', 'duration_sec', 'symbol_count', 'position_count', 'positions_present', 'reason', ...summaryColumns]
export const analysisTable = (columns: string[], rows: unknown[][] = []): AnalysisTable => ({ columns, rows })

export function metadataFixture(snapshotId: string | null = 'S1', changes: Partial<AnalysisMetadata> = {}): AnalysisMetadata {
  return { schema_version: 1, status: 'ready', source_version: 1, snapshot_id: snapshotId, snapshot_version: 1, logic_version: 'test', engine_version: 'test', computed_at: '2026-10-20T00:00:00', data_until: '2026-10-20T00:00:00', error: null, retry_at: null, read_view_schema_version: 1, ...changes }
}

export function chartFixture(accountId: number, snapshotId = 'S1', range: PerformanceRange = 'all'): AnalysisChartResponse {
  return {
    schema_version: 1, snapshot_id: snapshotId, account_id: accountId, range, snapshot_version: 1,
    computed_at: '2026-10-20T00:00:00', data_until: '2026-10-20T00:00:00', settings: { backtest_weight_type: 'cs', backtest_fee_rate: 0 },
    header: { range, engine_version: 'test', backtest_included: true, baseline: '2026-10-19T00:00:00', end: '2026-10-20T00:00:00', record_count: 2, observation_count: 2, used_record_count: 2, target_used_record_count: 2, invalid_asset_count: 0, sizing_fallback_count: 0, event_count: 0, skips: null, target_skips: null, calendar: { status: 'available', calendar_id: 'test', label: 'test' } },
    tables: {
      points: analysisTable(['date', 'observed_at', 'record_id', 'execution_id', 'account_return', 'portfolio_return', 'account_daily_return', 'portfolio_daily_return', 'target_portfolio_return', 'target_portfolio_daily_return'], [
        ['2026-10-19', '2026-10-19T00:00:00', 1, 'execution-1', 0, 0, null, null, 0, null],
        ['2026-10-20', '2026-10-20T00:00:00', 2, 'execution-2', 0.01, 0.0123456789012345, 0.01, 0.0123456789012345, 0.02, 0.02],
      ]),
      bindings: analysisTable(['time', 'portfolio_id']),
      fallbacks: analysisTable(['start', 'end', 'count']),
      calendar_ranges: analysisTable(['kind', 'start', 'end']),
      daily_costs: analysisTable(['day', ...summaryColumns, 'present']),
      daily_fees: analysisTable(['day', 'currency', 'value']),
      events: analysisTable(['time', 'tag', 'text', 'execution_id']),
      execution_index: analysisTable(indexColumns),
      execution_fees: analysisTable(['record_id', 'currency', 'value', 'count']),
    },
  }
}
