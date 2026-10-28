import { useCallback, useEffect, useMemo, useState } from 'react'
import { TimeSeriesChart, type CanvasFrame } from '@/components/viz/TimeSeriesChart'
import { drawCrosshair, drawSeries, drawTimeAxis, drawValueAxis, prepareCanvas, timeAtX, timeX, valueY, zoomTimeViewport } from '@/components/viz/chartCanvas'
import { niceValueAxis } from '@/components/viz/chartAxis'
import { seriesStyle } from '@/components/viz/seriesStyle'
import { clampViewport, nearestIndex, pointTime, timeLabel } from '@/features/history/chartModel'
import type { ContributionGroup } from '@/features/history/contributions'
import { amount } from '@/features/history/costs'
import { returnColor } from '@/features/history/performance'
import { createTimeScale, type TimeScaleMode } from '@/features/history/timeScale'
import { displayCurrencyUnit } from '@/lib/format'
import type { PerformancePoint } from '@/types/api'

const HEIGHT = 225
const plotBounds = (width: number) => ({ left: 12, right: width - 78, top: 16, bottom: 190 })

export function SeriesSwatch({ symbol }: { symbol: string }) {
  const style = seriesStyle(symbol)
  return <svg width="18" height="8" aria-hidden="true" className="inline-block shrink-0"><line x1="0" x2="18" y1="4" y2="4" stroke={style.color} strokeDasharray={style.dash.join(' ')} strokeWidth="1.8" /></svg>
}

export function ContributionChart({ rows, points, scaleMode, currency }: {
  rows: ContributionGroup[]; points: PerformancePoint[]; scaleMode: TimeScaleMode; currency: string | null
}) {
  const times = useMemo(() => points.map(pointTime), [points])
  const scale = useMemo(() => createTimeScale(scaleMode, times), [scaleMode, times])
  const full = useMemo(() => ({ start: times[0], end: times.at(-1)! }), [times])
  const [viewport, setViewport] = useState(full)
  const [hover, setHover] = useState<number | null>(null)
  useEffect(() => { setViewport(full); setHover(null) }, [full, scaleMode])
  const current = Math.min(hover ?? points.length - 1, points.length - 1)
  const axis = useMemo(() => niceValueAxis(rows.flatMap(row => row.values.filter((value, i): value is number => value != null && (times[i] >= viewport.start && times[i] <= viewport.end || times[i] < viewport.start && (times[i + 1] ?? Infinity) >= viewport.start || times[i] > viewport.end && (times[i - 1] ?? -Infinity) <= viewport.end))), 1), [rows, times, viewport])
  const segmented = rows.some(row => row.baselines.some(base => base != null && base > 0))

  const draw = useCallback((canvas: HTMLCanvasElement, frame: CanvasFrame) => {
    const ctx = prepareCanvas(canvas, frame.width, frame.height)
    if (!ctx) return
    const plot = plotBounds(frame.width), { theme } = frame, css = getComputedStyle(canvas)
    ctx.font = `11px ${theme.font}`; ctx.lineWidth = 1
    drawValueAxis(ctx, plot, axis, theme, value => Math.abs(value) >= 10000 ? `${(value / 10000).toFixed(1)}万` : amount(value))
    drawSeries(ctx, plot, times, rows.map(row => {
      const style = seriesStyle(row.symbol)
      return { values: row.values, baselines: row.baselines, color: css.getPropertyValue(style.color.slice(4, -1)).trim(), dash: style.dash, width: 1.8 }
    }), viewport, axis, scale)
    drawTimeAxis(ctx, plot, viewport, theme, 216, (time, intraday) => timeLabel(time).slice(intraday ? 11 : 0, intraday ? 16 : 10), scale)
    ctx.save(); ctx.beginPath(); ctx.rect(plot.left, plot.top - 3, plot.right - plot.left, plot.bottom - plot.top + 6); ctx.clip()
    rows.forEach(row => row.baselines.forEach((base, i) => {
      if (base !== i || i === 0) return
      ctx.beginPath(); ctx.arc(timeX(times[i], plot, viewport, scale), valueY(0, axis, plot), 3, 0, Math.PI * 2)
      ctx.fillStyle = theme.warn; ctx.fill()
    }))
    ctx.restore()
  }, [axis, rows, times, viewport, scale])

  const drawOverlay = useCallback((canvas: HTMLCanvasElement, frame: CanvasFrame) => {
    const ctx = prepareCanvas(canvas, frame.width, frame.height)
    if (!ctx || hover == null || times[current] < viewport.start || times[current] > viewport.end) return
    const value = rows.length === 1 ? rows[0].values[current] : null
    drawCrosshair(ctx, plotBounds(frame.width), timeX(times[current], plotBounds(frame.width), viewport, scale), frame.theme.muted, value == null ? undefined : valueY(value, axis, plotBounds(frame.width)))
  }, [hover, current, rows, times, viewport, scale, axis])

  const focusPoint = (index: number) => {
    setHover(index)
    if (times[index] < viewport.start || times[index] > viewport.end) {
      const span = viewport.end - viewport.start
      setViewport(clampViewport({ start: times[index] - span / 2, end: times[index] + span / 2 }, times))
    }
  }
  return <div className="min-w-0">
    <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2 text-xs"><span>{segmented ? '分段毛盈亏贡献' : '累计毛盈亏贡献'} · {displayCurrencyUnit(currency) || '按原币种'}</span><span className="text-ink-3">{segmented ? '各段起点归零' : '区间起点归零'}</span></div>
    <TimeSeriesChart height={HEIGHT} label="所选品种毛盈亏贡献曲线，方向键查看日期，异常处分段并从可靠快照重新归零" draw={draw} drawOverlay={drawOverlay}
      navigation={{ times, current, viewport, scale, plot: plotBounds, onFocus: focusPoint,
        onZoom: (factor, anchor = scale.invert((scale.project(viewport.start) + scale.project(viewport.end)) / 2)) => setViewport(view => zoomTimeViewport(view, factor, anchor, times, scale)),
        onReset: () => setViewport(full) }}
      interaction={{ 'data-testid': 'contribution-chart', 'aria-describedby': 'contribution-reading contribution-chart-hint',
        onPointerMove: event => {
          const rect = event.currentTarget.getBoundingClientRect(), plot = plotBounds(rect.width)
          const x = Math.max(plot.left, Math.min(plot.right, event.clientX - rect.left))
          setHover(nearestIndex(times, timeAtX(x, plot, viewport, scale)))
        }, onPointerLeave: () => setHover(null), onBlur: () => setHover(null) }} />
    <div id="contribution-reading" aria-live="polite" className="flex flex-wrap gap-x-4 gap-y-2 text-xs tabular-nums"><span className="text-ink-3">{timeLabel(times[current])}</span>{rows.map(row => {
      const base = row.baselines[current], value = row.values[current]
      return <span key={row.symbol}><SeriesSwatch symbol={row.symbol} /> {row.product} {base != null && base > 0 && '本段盈亏 '}<span className={returnColor(value)}>{value != null && value > 0 ? '+' : ''}{amount(value)}</span>{base != null && base > 0 && <span className="text-ink-3"> · 起点 {timeLabel(times[base]).slice(0, 16)}</span>}</span>
    })}</div>
    {rows.length === 0 ? <p role="status" className="mt-2 text-xs text-ink-3">选择品种查看曲线</p> : segmented ? <p className="mt-2 text-xs text-ink-3">圆点表示缺口后的新基准，后续显示本段盈亏；缺失区间未补齐。</p> : null}
    <p id="contribution-chart-hint" className="sr-only">方向键查看，Home / End 首末点，Ctrl + 滚轮或加减号缩放，双击或 0 恢复范围。{rows.flatMap(row => row.baselines.flatMap((base, i) => base === i && i > 0 ? [`${row.product} · ${timeLabel(times[i])} 数据缺口后重新归零。`] : [])).join('')}</p>
  </div>
}
