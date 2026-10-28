import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { PerformanceChart } from '../src/components/viz/PerformanceChart'
import { ContributionPanel } from '../src/features/history/ContributionPanel'
import type { ChartSelection } from '../src/features/history/chartModel'
import type { TimeScaleMode } from '../src/features/history/timeScale'
import type { AnalysisChart } from '../src/lib/api/performance'
import '../src/styles/theme.css'

const chart: AnalysisChart = {
  snapshot_id: 'fixture', snapshot_version: 1, range: 'all', computed_at: null, data_until: null,
  daily_costs: {}, events: [], event_count: 0,
  execution_index: [{ record_id: 4, execution_id: 'exec-4', time_ms: Date.parse('2026-10-07T15:00:00+08:00'),
    created_at: null, is_success: 1, noop: false, has_transactions: true, duration_sec: 1,
    symbol_count: 1, position_count: 1, positions_present: true, reason: null,
    summary: { value: 10000, cost: 0, lossBp: 0, coverage: 1, count: 1, covered: 1, amountComplete: true, fees: {}, feeCovered: 1 } }],
  result: {
    range: 'all', engine_version: 'test', backtest_included: true,
    settings: { backtest_weight_type: 'cs', backtest_fee_rate: 0 }, baseline: null, end: null,
    observation_count: 6, record_count: 6, used_record_count: 6, invalid_asset_count: 0, skips: null,
    bindings: [], calendar: { status: 'not_required', calendar_id: null, label: null, closed_ranges: [], unavailable_ranges: [] },
    points: [0, 0.02, -0.01, 0.08, 0.16, 0.13].map((value, i) => ({
      record_id: i + 1, date: `2026-10-${String(i * 2 + 1).padStart(2, '0')}`,
      observed_at: `2026-10-${String(i * 2 + 1).padStart(2, '0')}T15:00:00+08:00`,
      account_equity: 1000000 * (1 + value), account_return: value, portfolio_return: value * 1.1,
      account_daily_return: value / 3, portfolio_daily_return: value / 2, difference: value * 0.1,
    })),
  },
  contributions: ['DCE.c', 'SHFE.fu'].flatMap((product, j) => [0, 2000, -1000, 8000, 16000, 13000].map((value, idx) => ({
    idx, symbol: `${product}2701`, product, currency: 'CNY', gross_pnl: j ? -value : value,
    fee: 0, fee_missing: j ? 0 : 2, issues: j && idx >= 3 ? 1 : 0, trades: idx,
  }))),
}

export function Fixture() {
  const [selection, setSelection] = useState<ChartSelection>(null)
  const [scaleMode, setScale] = useState<TimeScaleMode>('observations')
  const [daily, setDaily] = useState(false)
  return <main className="h-full overflow-auto bg-bg p-6 text-ink-1">
    <div className="mb-4 flex gap-4 text-sm">
      <button onClick={() => document.documentElement.classList.toggle('dark')}>切换主题</button>
      <button onClick={() => setScale(scaleMode === 'natural' ? 'observations' : 'natural')}>切换时间轴</button>
      <button onClick={() => setDaily(!daily)}>切换每日</button>
    </div>
    <PerformanceChart accountId={7} data={chart.result} executionIndex={chart.execution_index} daily={daily} backtestMode="sized" scaleMode={scaleMode}
      costs={null} intervalCost={null} selection={selection} onSelect={setSelection} portfolioNames={new Map()} showExecutions />
    <output data-testid="selection">{JSON.stringify(selection)}</output>
    <ContributionPanel chart={chart} selection={selection} scaleMode={scaleMode} />
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
