import { Fragment, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ChevronRight, Info } from 'lucide-react'
import { Tooltip } from '@/components/ui/Tooltip'
import { displayCurrencyUnit } from '@/lib/format'
import type { AnalysisChart } from '@/lib/api/performance'
import type { ChartSelection } from '@/features/history/chartModel'
import { contributionView } from '@/features/history/contributions'
import { createTimeScale, type TimeScaleMode } from '@/features/history/timeScale'
import { pointTime } from '@/features/history/chartModel'
import { amount } from '@/features/history/costs'
import { returnColor } from '@/features/history/performance'
import { OverflowText } from '@/components/ui/OverflowText'

function paths(values: (number | null)[], x: (i: number) => number, y: (v: number) => number) {
  let connected = false
  return values.map((v, i) => {
    if (v == null) { connected = false; return '' }
    const command = connected ? 'L' : 'M'; connected = true
    return `${command}${x(i)},${y(v)}`
  }).join(' ')
}

export function ContributionPanel({ chart, selection, scaleMode }: {
  chart: AnalysisChart; selection: ChartSelection; scaleMode: TimeScaleMode
}) {
  const view = useMemo(() => contributionView(chart, selection), [chart, selection])
  const [chosen, setChosen] = useState<string[] | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [hover, setHover] = useState<number | null>(null)
  const plot = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(520)
  useLayoutEffect(() => {
    const element = plot.current
    if (!element) return
    const measure = () => setWidth(Math.max(1, element.getBoundingClientRect().width))
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
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
  const incomplete = groups.some(row => row.gross == null)
  return <section className="border-t border-line py-3 [&_button]:min-h-[30px]!" aria-label="品种贡献">
    <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
      <h2 className="text-sm font-medium">品种贡献</h2>
      <span className="text-xs text-ink-3">{displayCurrencyUnit(view.currency) || '按原币种'}</span>
      <Tooltip content={<div className="space-y-1 text-xs">
        <p>期初权益 {amount(points[0].account_equity ?? null)} · 期末权益 {amount(points.at(-1)!.account_equity ?? null)}</p>
        <p>毛盈亏含已平仓与持仓浮盈亏，净盈亏仅在费用完整时显示。</p>
        <p>差额包含未获取费用、出入金及缺失数据；不重复扣除滑点。</p>
      </div>} arrow>
        <button type="button" aria-label="品种贡献计算说明" className="inline-flex w-[30px] items-center justify-center rounded text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent"><Info size={14} aria-hidden="true" /></button>
      </Tooltip>
      {incomplete && <span role="status" className="text-xs text-warn">部分品种未计入毛盈亏汇总</span>}
    </div>
    <dl className="mb-2 flex flex-wrap gap-x-5 gap-y-1 text-xs tabular-nums">
      {[
        ['权益变化', view.accountChange], ['已知毛盈亏', view.knownGross],
        ['已知手续费', view.knownFees], ['未归因差额', view.unexplained],
      ].map(([label, value]) => <div key={String(label)} className="flex items-baseline gap-2"><dt className="text-ink-3">{label}</dt><dd>{amount(value as number | null)}</dd></div>)}
      {groups.some(row => row.feeMissing > 0) && <div className="text-ink-3"><dt className="sr-only">费用状态</dt><dd>手续费未完整</dd></div>}
    </dl>
    {chart.contributions == null ? <p className="text-sm text-ink-3">当前快照尚无品种贡献，请刷新绩效。</p> : groups.length === 0 ? <p className="text-sm text-ink-3">本区间无可归因品种。</p> : <div className="grid min-w-0 items-start gap-4 lg:grid-cols-[minmax(22rem,1.2fr)_minmax(0,1.8fr)]">
      <div className="min-w-0 overflow-x-auto">
        <table className="w-full text-left text-xs tabular-nums"><caption className="sr-only">品种贡献排名，按毛盈亏从高到低；包含已清仓合约</caption>
          <thead className="text-ink-3"><tr className="border-b border-line"><th className="py-2 font-normal">品种</th><th className="py-2 pr-3 text-right font-normal">毛盈亏</th><th className="py-2 pr-3 text-right font-normal">净盈亏</th><th className="py-2 font-normal">状态</th></tr></thead>
          <tbody>{groups.map(row => <Fragment key={row.symbol}>
            <tr className="border-b border-line">
              <td className="py-1 pr-3"><div className="flex items-center gap-1">
                <label className="flex min-h-[30px] min-w-0 items-center gap-2"><input type="checkbox" checked={selected.includes(row.symbol)} onChange={() => toggle(row.symbol)} aria-label={`查看 ${row.product} 贡献曲线`} /><OverflowText text={row.product} /></label>
                <button type="button" aria-label={`${expanded === row.symbol ? '收起' : '展开'} ${row.product} 合约与数据状态`} aria-expanded={expanded === row.symbol} className="inline-flex shrink-0 items-center gap-0.5 rounded px-1 text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent" onClick={() => setExpanded(expanded === row.symbol ? null : row.symbol)}>
                  <span>{row.contracts.length}合约</span><ChevronRight size={12} aria-hidden="true" className={`transition-transform duration-200 motion-reduce:transition-none ${expanded === row.symbol ? 'rotate-90' : ''}`} />
                </button>
              </div></td>
              <td className={`whitespace-nowrap py-1 pr-3 text-right ${returnColor(row.gross)}`}>{amount(row.gross)}</td>
              <td className={`whitespace-nowrap py-1 pr-3 text-right ${returnColor(row.net)}`}>{amount(row.net)}</td>
              <td className="whitespace-nowrap py-1 text-ink-3">
                {row.gross == null ? <span className={row.issues > 0 ? 'text-warn' : undefined}>{row.issues > 0 ? '待核对' : '估值未知'}</span> : null}
                {row.gross == null && row.feeMissing > 0 && ' · '}
                {row.feeMissing > 0 ? '费用缺失' : row.gross != null ? '—' : null}
              </td>
            </tr>
            <tr><td colSpan={4} className="p-0">
              <div className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expanded === row.symbol ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden" inert={expanded !== row.symbol} aria-hidden={expanded !== row.symbol}>
                <div className="space-y-1 border-b border-line px-2 py-2 text-ink-2">
                  {row.gross == null && <p className={row.issues > 0 ? 'text-warn' : 'text-ink-3'}>{row.issues > 0 ? '成交／持仓待核对，毛盈亏未计入汇总。' : '估值未获取，毛盈亏未计入汇总。'}</p>}
                  <p className="text-ink-3">{row.feeMissing > 0 ? `${row.feeMissing} 笔费用未知，净盈亏暂不可用。已知费用 ${amount(row.fee)}` : `费用 ${amount(row.fee)}`} {displayCurrencyUnit(row.currency)}</p>
                  {row.contracts.map(c => <div key={c.symbol} className="flex justify-between gap-3"><span>{c.symbol}</span><span>{amount(c.gross)} {displayCurrencyUnit(c.currency)}</span></div>)}
                </div>
              </div></div>
            </td></tr>
          </Fragment>)}</tbody>
        </table>
      </div>
      <div ref={plot} className="min-w-0">
        <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2 text-xs"><span>累计毛盈亏贡献 · {displayCurrencyUnit(view.currency) || '按原币种'}</span><span className="text-ink-3">区间起点归零</span></div>
        <svg className="block h-[225px] w-full" viewBox={`0 0 ${width} 225`} height="225" preserveAspectRatio="none" role="group" tabIndex={0} aria-label="所选品种累计毛盈亏贡献曲线，方向键查看日期，缺失数据处断线" onKeyDown={event => {
          const next = event.key === 'ArrowLeft' ? Math.max(0, current - 1) : event.key === 'ArrowRight' ? Math.min(points.length - 1, current + 1) : event.key === 'Home' ? 0 : event.key === 'End' ? points.length - 1 : null
          if (next != null) { event.preventDefault(); setHover(next) }
        }} onPointerLeave={() => setHover(null)} onPointerMove={event => {
          const rect = event.currentTarget.getBoundingClientRect(), px = (event.clientX - rect.left) / rect.width * width
          let nearest = 0
          times.forEach((_, i) => { if (Math.abs(x(i) - px) < Math.abs(x(nearest) - px)) nearest = i })
          setHover(nearest)
        }}>
          {[min, (max + min) / 2, max].filter((v, i, allValues) => allValues.indexOf(v) === i).map(v => <g key={v}><line x1={left} x2={right} y1={y(v)} y2={y(v)} stroke="var(--color-line)" /><text x={left - 6} y={y(v) + 4} textAnchor="end" fill="var(--color-ink-2)" fontSize="11">{Math.abs(v) >= 10000 ? `${(v / 10000).toFixed(1)}万` : amount(v)}</text></g>)}
          {visible.map((row, i) => <path key={row.symbol} d={paths(row.values, x, y)} fill="none" stroke={row.gross != null && row.gross < 0 ? 'var(--color-down)' : 'var(--color-up)'} strokeWidth="1.5" vectorEffect="non-scaling-stroke" strokeDasharray={i === 0 ? undefined : `${6 + i * 2} 4`} />)}
          {hover != null && <line x1={x(current)} x2={x(current)} y1={top} y2={bottom} stroke="var(--color-ink-3)" strokeDasharray="3 3" />}
          <text x={left} y="216" fill="var(--color-ink-2)" fontSize="11">{points[0].date.slice(0, 10)}</text><text x={right} y="216" textAnchor="end" fill="var(--color-ink-2)" fontSize="11">{points.at(-1)!.date.slice(0, 10)}</text>
        </svg>
        <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs"><span className="text-ink-3">{points[current].date.slice(0, 10)}</span>{visible.map((row, i) => <span key={row.symbol}><span aria-hidden="true">{i ? '┄' : '━'} </span>{row.product} <span className={returnColor(row.values[current])}>{amount(row.values[current])}</span></span>)}</div>
        {visible.length === 0 ? <p role="status" className="mt-2 text-xs text-ink-3">选择品种查看曲线</p> : visible.some(row => row.gross == null) ? <p role="status" className="mt-2 text-xs text-warn">所选品种数据不完整，仅展示可用时段，缺失处断线。</p> : null}
      </div>
    </div>}
  </section>
}
