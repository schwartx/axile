import { useMemo } from 'react'
import type { AnalysisChart } from '@/lib/api/performance'
import { ContributionChart } from './ContributionChart'
import { contributionView } from './contributions'
import { pointTime, type ChartSelection } from './chartModel'
import type { TimeScaleMode } from './timeScale'
import type { LocalInputs } from './localAnalysis'

export function SymbolPerformance({ chart, selection, symbol, samples, currency, scaleMode, inputs, onInterval }: {
  chart: AnalysisChart; selection: ChartSelection; symbol: string; samples: [number, number][] | null
  currency: string | null; scaleMode: TimeScaleMode; inputs: LocalInputs | null
  onInterval: (start: number, end: number) => void
}) {
  const view = useMemo(() => contributionView(chart, selection), [chart, selection])
  const live = view?.groups.flatMap(group => group.contracts).find(contract => contract.symbol === symbol)
  const comparable = !live || live.currency === currency
  const rows = useMemo(() => {
    const curves = live ? [{ ...live, symbol: 'actual', product: '实盘' }] : []
    if (!view || !samples?.length || !comparable) return curves
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
    return [...curves, { symbol: 'backtest', product: '回测', values, baselines }]
  }, [view, live, samples, comparable])
  const tradeTimes = useMemo(() => inputs?.records.flatMap(record => record.trades.flatMap(raw => {
    const trade = raw as { symbol: string; time_us: number }
    return trade.symbol === symbol ? [trade.time_us / 1000] : []
  })), [inputs, symbol])
  return <>
    {!live && <p className="text-xs text-ink-2">本区间缺少该品种的实盘盈亏观测。</p>}
    {!comparable && <p className="text-xs text-ink-2">实盘与回测计价币种不同，无法同轴比较。</p>}
    {view && rows.length > 0 && <ContributionChart rows={rows} points={view.points} scaleMode={scaleMode} currency={live?.currency ?? currency} height={480} tradeTimes={live ? tradeTimes : undefined} onInterval={onInterval} />}
  </>
}
