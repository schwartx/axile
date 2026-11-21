import { useEffect, useMemo, type ReactNode } from 'react'
import type { AnalysisChart } from '@/lib/api/performance'
import { CHART_HEIGHT } from '@/components/viz/performanceCanvas'
import { ContributionChart } from './ContributionChart'
import { PerformanceChartPlaceholder } from './PerformanceChartPlaceholder'
import { contributionView } from './contributions'
import { navigationTimes, pointTime, type ChartSelection } from './chartModel'
import type { TimeScaleMode } from './timeScale'
import { useChannelDescriptor } from '@/stores/channels'
import type { RangeTarget } from './rangeZoom'

function symbolCurves(chart: AnalysisChart, symbol: string, samples: [number, number][] | null, currency: string | null) {
  const view = contributionView(chart, null)
  if (!view) return null
  const live = view.groups.flatMap(group => group.contracts).find(contract => contract.symbol === symbol)
  const comparable = !live || live.currency === currency
  const rows = live ? [{ ...live, symbol: 'actual', product: '实盘' }] : []
  if (samples?.length && comparable) {
    let next = 0, baseIndex = -1, baseValue = 0
    const baselines: (number | null)[] = []
    const values = view.points.map((point, index) => {
      const time = pointTime(point)
      while (next < samples.length && Math.floor(samples[next][0] / 1000) <= time) next++
      if (!next) { baselines.push(null); return null }
      const gross = samples[next - 1][1]
      if (baseIndex < 0 || live?.baselines[index] === index) { baseIndex = index; baseValue = gross }
      baselines.push(baseIndex)
      return gross - baseValue
    })
    return { view, live, comparable, rows: [...rows, { symbol: 'backtest', product: '回测', values, baselines }] }
  }
  return { view, live, comparable, rows }
}

export function SymbolPerformance({ chart, rangeTarget, targetSamples, selection, slideInterval, symbol, samples, currency, scaleMode, accountId, channel, showExecutions, onInterval, heading }: {
  chart: AnalysisChart; selection: ChartSelection; symbol: string; samples: [number, number][] | null
  rangeTarget?: RangeTarget; targetSamples?: [number, number][] | null
  currency: string | null; scaleMode: TimeScaleMode; accountId: number; channel?: string | null; showExecutions: boolean
  slideInterval?: boolean
  onInterval: (start: number, end: number) => void
  heading: ReactNode
}) {
  const units = useChannelDescriptor(channel)?.units
  const curves = useMemo(() => symbolCurves(chart, symbol, samples, currency), [chart, symbol, samples, currency])
  const domain = useMemo(() => navigationTimes(chart.result.points, chart.execution_index), [chart])
  const target = useMemo(() => {
    if (!rangeTarget) return
    const next = symbolCurves(rangeTarget.chart, symbol, targetSamples ?? null, currency)
    if (!next || (next.live?.currency ?? currency) !== (curves?.live?.currency ?? currency)) return
    return {
      finish: rangeTarget.finish, rows: next.rows, times: next.view.points.map(pointTime),
      domain: navigationTimes(next.view.points, rangeTarget.chart.execution_index),
      executions: rangeTarget.chart.execution_index.filter(row => row.trade_symbols.includes(symbol)),
    }
  }, [rangeTarget, symbol, targetSamples, currency, curves])
  useEffect(() => { if (rangeTarget && (!curves || !target)) rangeTarget.finish() }, [rangeTarget, curves, target])
  const executions = useMemo(() => chart.execution_index.filter(row => row.trade_symbols.includes(symbol)), [chart.execution_index, symbol])
  if (!curves) return <PerformanceChartPlaceholder accountId={accountId} loading={false} failed={false} message="有效观测不足两条" />
  const { rows, view, live, comparable } = curves
  return <ContributionChart rows={rows} points={view.points} navigationDomain={domain} rangeTarget={target} scaleMode={scaleMode}
    currency={live?.currency ?? currency} height={CHART_HEIGHT} onInterval={onInterval} selection={selection} slideInterval={slideInterval}
    heading={<>
      {heading}
      {!live && <p className="text-xs text-ink-2">本区间缺少该品种的实盘盈亏观测。</p>}
      {!comparable && <p className="text-xs text-ink-2">实盘与回测计价币种不同，无法同轴比较。</p>}
    </>}
    executions={{ rows: executions, symbol, accountId, snapshotId: chart.version, range: chart.range, units, visible: showExecutions }} />
}
