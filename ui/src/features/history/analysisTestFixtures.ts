import type { AnalysisChartResponse, AnalysisMetadata, AnalysisTable, PerformanceRange } from '@/lib/api/performance'

export const summaryColumns = ['value', 'cost', 'loss_bp', 'coverage', 'covered', 'count', 'amount_complete', 'fee_covered', 'covered_value', 'estimated']
export const indexColumns = ['record_id', 'execution_id', 'time', 'created_at', 'is_success', 'noop', 'has_transactions', 'duration_sec', 'symbol_count', 'position_count', 'positions_present', 'reason', ...summaryColumns]
export const analysisTable = (columns: string[], rows: unknown[][] = []): AnalysisTable => ({ columns, rows })

export function metadataFixture(snapshotId: string | null = 'S1', changes: Partial<AnalysisMetadata> = {}): AnalysisMetadata {
  return { schema_version: 1, status: 'ready', source_version: 1, version: snapshotId, logic_version: 'test', engine_version: 'test', computed_at: '2026-10-20T00:00:00', data_until: '2026-10-20T00:00:00', error: null, retry_at: null, ...changes }
}

export function chartFixture(accountId = 1, snapshotId = 'S1', range: PerformanceRange = 'all'): AnalysisChartResponse {
  return {
    schema_version: 1, version: snapshotId, account_id: accountId, range,
    computed_at: '2026-10-20T00:00:00', data_until: '2026-10-20T00:00:00',
    result: {
      range, settings: { backtest_weight_type: 'cs', backtest_fee_rate: 0 }, engine_version: 'test', backtest_included: true,
      baseline: '2026-10-19T00:00:00', end: '2026-10-20T00:00:00', record_count: 2, observation_count: 2, used_record_count: 2, target_used_record_count: 2,
      invalid_asset_count: 0, sizing_fallback_count: 0, skips: null, target_skips: null,
      points: [
        {date:'2026-10-19',observed_at:'2026-10-19T00:00:00',record_id:1,execution_id:'execution-1',difference:0,account_return:0,portfolio_return:0,account_daily_return:null,portfolio_daily_return:null,target_portfolio_return:0,target_portfolio_daily_return:null},
        {date:'2026-10-20',observed_at:'2026-10-20T00:00:00',record_id:2,execution_id:'execution-2',difference:-0.0023456789012345,account_return:0.01,portfolio_return:0.0123456789012345,account_daily_return:0.01,portfolio_daily_return:0.0123456789012345,target_portfolio_return:0.02,target_portfolio_daily_return:0.02},
      ], bindings: [], calendar: {status:'unavailable',calendar_id:null,label:null,closed_ranges:[],unavailable_ranges:[]},
    },
    daily_costs: {}, events: [], event_count: 0, execution_index: [],
  }
}
