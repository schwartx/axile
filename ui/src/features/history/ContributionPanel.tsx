import { useEffect, useMemo, useRef, useState } from 'react'
import type { AnalysisChart } from '@/lib/api/performance'
import type { ChartSelection } from '@/features/history/chartModel'
import { contributionView } from '@/features/history/contributions'
import { createTimeScale, type TimeScaleMode } from '@/features/history/timeScale'
import { pointTime } from '@/features/history/chartModel'
import { amount, shanghaiLabel } from '@/features/history/costs'
import { returnColor, returnText } from '@/features/history/performance'
import { OverflowText } from '@/components/ui/OverflowText'

function paths(values: (number | null)[], x: (i: number) => number, y: (v: number) => number) {
  let connected = false
  return values.map((v, i) => {
    if (v == null) { connected = false; return '' }
    const command = connected ? 'L' : 'M'; connected = true
    return `${command}${x(i)},${y(v)}`
  }).join(' ')
}

export function ContributionPanel({ chart, selection, onSelect, scaleMode }: {
  chart: AnalysisChart; selection: ChartSelection; onSelect: (value: ChartSelection) => void; scaleMode: TimeScaleMode
}) {
  const view = useMemo(() => contributionView(chart, selection), [chart, selection])
  const [chosen, setChosen] = useState<string[] | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [hover, setHover] = useState<number | null>(null)
  const plot = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(520)
  useEffect(() => {
    if (!plot.current) return
    const observer = new ResizeObserver(entries => setWidth(Math.max(260, entries[0].contentRect.width)))
    observer.observe(plot.current)
    return () => observer.disconnect()
  }, [view?.groups.length, chart.snapshot_id])
  if (!view) return null
  const { groups, points } = view
  const defaults = groups.filter(r => r.gross != null)
  const selected = chosen ?? [...new Set([defaults[0]?.symbol, defaults.at(-1)?.symbol].filter((s): s is string => !!s))]
  const visible = groups.filter(row => selected.includes(row.symbol))
  const times = points.map(pointTime), timeScale = createTimeScale(scaleMode, times)
  const left = 62, right = width - 12, top = 22, bottom = 190
  const a = timeScale.project(times[0]), b = timeScale.project(times.at(-1)!)
  const x = (i: number) => left + (timeScale.project(times[i]) - a) / (b - a || 1) * (right - left)
  const all = visible.flatMap(row => row.values.filter((v): v is number => v != null))
  const min = all.reduce((a, b) => Math.min(a, b), 0), max = all.reduce((a, b) => Math.max(a, b), 0), span = max - min || 1
  const y = (v: number) => bottom - (v - min) / span * (bottom - top)
  const current = hover == null ? points.length - 1 : Math.min(hover, points.length - 1)
  const toggle = (symbol: string) => setChosen(selected.includes(symbol) ? selected.filter(s => s !== symbol) : [...selected, symbol])
  const boundary = `${shanghaiLabel(points[0].observed_at ?? points[0].date)} → ${shanghaiLabel(points.at(-1)!.observed_at ?? points.at(-1)!.date)}`
  return <section className="border-t border-line py-4" aria-label="品种贡献">
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="text-sm font-medium">品种贡献</h2>
      <span className="text-xs text-ink-3">{selection?.kind === 'interval' ? '选定区间' : '当前范围'} · {boundary}</span>
    </div>
    <dl className="mb-3 flex flex-wrap gap-x-6 gap-y-2 text-xs tabular-nums">
      {[
        ['期初权益', points[0].account_equity], ['期末权益', points.at(-1)!.account_equity],
        ['权益变化', view.accountChange], ['已知品种毛盈亏', view.knownGross],
        ['已知手续费', view.knownFees], ['未归因差额', view.unexplained],
      ].map(([label, value]) => <div key={String(label)}><dt className="text-ink-3">{label}</dt><dd className="mt-1">{amount(value as number | null)} {view.currency ?? ''}</dd></div>)}
    </dl>
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
      <span className="text-ink-3">区间最大回撤 {returnText(view.maxDrawdown)}（观测级）</span>
      {view.drawdownSelection && <button type="button" className="min-h-9 text-accent" onClick={() => onSelect(view.drawdownSelection)}>分析这段回撤</button>}
      <span className="text-ink-3">差额包含未获取费用、出入金及缺失数据；不重复扣除滑点。</span>
    </div>
    <svg className="mb-4 w-full" viewBox={`0 0 ${points.length - 1} 60`} height="60" preserveAspectRatio="none" role="img" aria-label="账户区间回撤曲线，按现有权益观测计算">
      <path d={paths(view.drawdowns, i => i, v => 5 + v / (view.maxDrawdown || -1) * 50)} fill="none" stroke="var(--color-down)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
    {chart.contributions == null ? <p className="text-sm text-ink-3">当前快照尚无品种贡献，请刷新绩效。</p> : groups.length === 0 ? <p className="text-sm text-ink-3">本区间无可归因品种。</p> : <div className="grid min-w-0 gap-5 lg:grid-cols-[minmax(16rem,1fr)_minmax(0,2fr)]">
      <div className="min-w-0">
        <table className="w-full text-left text-xs tabular-nums"><caption className="sr-only">品种贡献排名，按毛盈亏从高到低；包含已清仓合约</caption>
          <thead className="text-ink-3"><tr className="border-b border-line"><th className="py-2 font-normal">品种</th><th className="py-2 text-right font-normal">毛盈亏</th><th className="py-2 text-right font-normal">净盈亏</th></tr></thead>
          <tbody>{groups.map(row => <tr key={row.symbol} className="border-b border-line align-top"><td className="py-2 pr-2">
            <label className="flex min-h-9 items-center gap-2"><input type="checkbox" checked={selected.includes(row.symbol)} onChange={() => toggle(row.symbol)} aria-label={`查看 ${row.product} 贡献曲线`} /><OverflowText text={row.product} /></label>
            <button type="button" aria-expanded={expanded === row.symbol} className="min-h-9 text-ink-3 hover:text-ink-1" onClick={() => setExpanded(expanded === row.symbol ? null : row.symbol)}>{row.contracts.length} 个合约{expanded === row.symbol ? ' · 收起' : ' · 展开'}</button>
            <div className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expanded === row.symbol ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden" inert={expanded !== row.symbol} aria-hidden={expanded !== row.symbol}>{row.contracts.map(c => <div key={c.symbol} className="py-1 text-ink-2">{c.symbol} {amount(c.gross)} {c.currency ?? ''}</div>)}</div></div>
          </td><td className={`py-3 text-right ${returnColor(row.gross)}`}>{amount(row.gross)}{row.gross == null && <div className="mt-1 text-warn">{row.issues > 0 ? '成交／持仓待核对' : '估值未获取'}</div>}</td><td className={`py-3 text-right ${returnColor(row.net)}`}>{amount(row.net)}<div className="mt-1 text-ink-3">{row.feeMissing ? `${row.feeMissing} 笔费用未知` : `费用 ${amount(row.fee)}`}</div></td></tr>)}</tbody>
        </table>
      </div>
      <div ref={plot} className="min-w-0">
        <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2 text-xs"><span>累计盈亏贡献 · {view.currency ?? '按原币种'}</span><span className="text-ink-3">区间起点归零</span></div>
        <svg viewBox={`0 0 ${width} 225`} width="100%" role="group" tabIndex={0} aria-label="所选品种累计毛盈亏贡献曲线，方向键查看日期，缺失数据处断线" onKeyDown={event => {
          const next = event.key === 'ArrowLeft' ? Math.max(0, current - 1) : event.key === 'ArrowRight' ? Math.min(points.length - 1, current + 1) : event.key === 'Home' ? 0 : event.key === 'End' ? points.length - 1 : null
          if (next != null) { event.preventDefault(); setHover(next) }
        }} onPointerLeave={() => setHover(null)} onPointerMove={event => {
          const rect = event.currentTarget.getBoundingClientRect(), px = (event.clientX - rect.left) / rect.width * width
          let nearest = 0
          times.forEach((_, i) => { if (Math.abs(x(i) - px) < Math.abs(x(nearest) - px)) nearest = i })
          setHover(nearest)
        }}>
          {[min, (max + min) / 2, max].filter((v, i, allValues) => allValues.indexOf(v) === i).map(v => <g key={v}><line x1={left} x2={right} y1={y(v)} y2={y(v)} stroke="var(--color-line)" /><text x={left - 6} y={y(v) + 4} textAnchor="end" fill="var(--color-ink-2)" fontSize="11">{Math.abs(v) >= 10000 ? `${(v / 10000).toFixed(1)}万` : amount(v)}</text></g>)}
          {visible.map((row, i) => <path key={row.symbol} d={paths(row.values, x, y)} fill="none" stroke={row.gross != null && row.gross < 0 ? 'var(--color-down)' : 'var(--color-up)'} strokeWidth="2" strokeDasharray={i === 0 ? undefined : `${6 + i * 2} 4`} />)}
          {hover != null && <line x1={x(current)} x2={x(current)} y1={top} y2={bottom} stroke="var(--color-ink-3)" strokeDasharray="3 3" />}
          <text x={left} y="216" fill="var(--color-ink-2)" fontSize="11">{points[0].date.slice(0, 10)}</text><text x={right} y="216" textAnchor="end" fill="var(--color-ink-2)" fontSize="11">{points.at(-1)!.date.slice(0, 10)}</text>
        </svg>
        <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs"><span className="text-ink-3">{points[current].date.slice(0, 10)}</span>{visible.map((row, i) => <span key={row.symbol}><span aria-hidden="true">{i ? '┄' : '━'} </span>{row.product} <span className={returnColor(row.values[current])}>{amount(row.values[current])}</span></span>)}</div>
        <p className="mt-3 text-xs text-ink-3">勾选排名中的品种对比；毛盈亏含已平仓与持仓浮盈亏，净盈亏仅在费用完整时显示。</p>
      </div>
    </div>}
  </section>
}
