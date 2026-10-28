import type { AnalysisChart, ContributionObservation } from '@/lib/api/performance'
import { pointTime, type ChartSelection } from '@/features/history/chartModel'

export interface ContributionSeries {
  symbol: string
  product: string
  currency: string | null
  gross: number | null
  fee: number
  feeMissing: number
  net: number | null
  issues: number
  trades: number
  values: (number | null)[]
  baselines: (number | null)[]
}
export interface ContributionGroup extends ContributionSeries { contracts: ContributionSeries[] }
const zero = { gross_pnl: 0, fee: 0, fee_missing: 0, issues: 0, trades: 0 }
const difference = (a: number | null | undefined, b: number | null | undefined) => a == null || b == null ? null : a - b
const sum = (values: (number | null)[]) => values.some(v => v == null) ? null : values.reduce<number>((a, b) => a + b!, 0)
type Sample = Pick<ContributionObservation, 'gross_pnl' | 'issues'>
type ContractSeries = ContributionSeries & { samples: (Sample | null)[] }

function segmentCurve(samples: (Sample | null)[]) {
  let base: { idx: number; value: number } | null = null
  let previousIssues: number | null = null
  const baselines: (number | null)[] = []
  const values = samples.map((row, idx) => {
    const changed = previousIssues != null && row?.issues !== previousIssues
    previousIssues = row?.issues ?? null
    if (row?.gross_pnl == null) { base = null; baselines.push(null); return null }
    if (base == null || changed) base = { idx, value: row.gross_pnl }
    baselines.push(base.idx)
    return row.gross_pnl - base.value
  })
  return { values, baselines }
}

function contractSeries(observations: ContributionObservation[], start: number, end: number) {
  const rows = new Map<string, Map<number, ContributionObservation>>()
  for (const row of observations) {
    const values = rows.get(row.symbol) ?? new Map<number, ContributionObservation>()
    values.set(row.idx, row); rows.set(row.symbol, values)
  }
  const contracts: ContractSeries[] = []
  for (const [symbol, values] of rows) {
    const first = Math.min(...values.keys())
    if (first > end) continue
    const base = values.get(start) ?? (first > start ? zero : null)
    const last = values.get(end)
    if (!last) continue
    const issues = base == null ? 1 : Math.max(0, last.issues - base.issues)
    const feeMissing = base == null ? 1 : Math.max(0, last.fee_missing - base.fee_missing)
    const fee = base == null ? 0 : last.fee - base.fee
    const gross = issues > 0 || base == null ? null : difference(last.gross_pnl, base.gross_pnl)
    const samples = Array.from({ length: end - start + 1 }, (_, i) => values.get(i + start) ?? (i + start < first ? zero : null))
    contracts.push({ symbol, product: last.product, currency: last.currency, gross, fee, feeMissing, issues,
      net: gross != null && feeMissing === 0 ? gross - fee : null, trades: base == null ? 0 : last.trades - base.trades,
      samples, ...segmentCurve(samples),
    })
  }
  return contracts
}

function groupSeries(contracts: ContractSeries[], length: number) {
  const grouped = new Map<string, ContractSeries[]>()
  for (const row of contracts) {
    const key = `${row.currency ?? 'unknown'}:${row.product}`
    grouped.set(key, [...(grouped.get(key) ?? []), row])
  }
  const groups: ContributionGroup[] = [...grouped].map(([key, items]) => ({
    symbol: key, product: items[0].product, currency: items[0].currency, contracts: items,
    gross: sum(items.map(r => r.gross)), fee: items.reduce((a, r) => a + r.fee, 0),
    feeMissing: items.reduce((a, r) => a + r.feeMissing, 0), issues: items.reduce((a, r) => a + r.issues, 0),
    net: sum(items.map(r => r.net)), trades: items.reduce((a, r) => a + r.trades, 0),
    // 同品种先汇总同一观测的原始值，再统一归零，避免相加不同起点的分段盈亏。
    ...segmentCurve(Array.from({ length }, (_, i) => {
      const samples = items.map(r => r.samples[i])
      return samples.some(r => r == null) ? null : {
        gross_pnl: sum(samples.map(r => r!.gross_pnl)), issues: samples.reduce((a, r) => a + r!.issues, 0),
      }
    })),
  })).sort((a, b) => a.gross == null ? b.gross == null ? a.product.localeCompare(b.product) : 1 : b.gross == null ? -1 : b.gross - a.gross)
  return groups
}

/** Selection rebases frozen backend P&L, preserving its currency and quality counters. */
export function contributionView(chart: AnalysisChart, selection: ChartSelection) {
  const points = chart.result.points
  const start = selection?.kind === 'interval' ? points.findIndex(p => pointTime(p) === selection.start) : 0
  const end = selection?.kind === 'interval' ? points.findIndex(p => pointTime(p) === selection.end) : points.length - 1
  if (start < 0 || end <= start) return null
  const groups = groupSeries(contractSeries(chart.contributions ?? [], start, end), end - start + 1)
  const currencies = new Set(groups.map(g => g.currency))
  const accountChange = difference(points[end].account_equity, points[start].account_equity)
  const knownGross = groups.reduce((a, r) => a + (r.gross ?? 0), 0)
  const knownFees = groups.reduce((a, r) => a + r.fee, 0)
  const comparable = groups.length > 0 && currencies.size === 1 && !currencies.has(null)
  return { start, end, points: points.slice(start, end + 1), groups, accountChange,
    knownGross: comparable ? knownGross : null, knownFees: comparable ? knownFees : null,
    unexplained: comparable && accountChange != null ? accountChange - knownGross + knownFees : null,
    currency: currencies.size === 1 ? groups[0]?.currency : null,
  }
}
