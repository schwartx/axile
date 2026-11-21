import { useCallback, useEffect, useMemo, useRef, useState, useId, type PointerEvent, type ReactNode } from 'react'
import { TimeSeriesChart, type CanvasFrame, type WindowMotion } from '@/components/viz/TimeSeriesChart'
import { drawCrosshair, drawSeries, drawTimeAxis, drawValueAxis, prepareCanvas, timeAtX, timeX, valueY, zoomTimeViewport } from '@/components/viz/chartCanvas'
import { niceValueAxis } from '@/components/viz/chartAxis'
import { seriesStyle } from '@/components/viz/seriesStyle'
import { clampViewport, intervalSelection, nearestIndex, pointTime, timeLabel, type ChartSelection, type Viewport } from '@/features/history/chartModel'
import { rangeContributionRows, rangeExecutions, rangeZoom, type RangeCurve } from './rangeZoom'
import { ChartTradingOverlay } from './ChartTradingOverlay'
import { drawExecutionMarkers, drawExecutionRing, drawSelectionGuide, useExecutionMarkerInteraction } from './executionMarkers'
import { amount } from '@/features/history/costs'
import { returnColor } from '@/features/history/performance'
import { createTimeScale, type TimeScale, type TimeScaleMode } from '@/features/history/timeScale'
import { displayCurrencyUnit } from '@/lib/format'
import type { AnalysisExecutionSummary, PerformanceRange } from '@/lib/api/performance'
import type { ChannelCapability, PerformancePoint } from '@/types/api'

interface SymbolExecutions {
  rows: AnalysisExecutionSummary[]; symbol: string; accountId: number; snapshotId: string; range: PerformanceRange
  units?: ChannelCapability['units']; visible: boolean
}

export function SeriesSwatch({ symbol }: { symbol: string }) {
  const style = seriesStyle(symbol)
  return <svg width="18" height="8" aria-hidden="true" className="inline-block shrink-0"><line x1="0" x2="18" y1="4" y2="4" stroke={style.color} strokeDasharray={style.dash.join(' ')} strokeWidth="1.8" /></svg>
}

export function ContributionChart({ rows, points, navigationDomain, rangeTarget, scaleMode, currency, height = 225, onInterval, executions, heading, selection, slideInterval }: {
  height?: number; onInterval?: (start: number, end: number) => void; executions?: SymbolExecutions; heading?: ReactNode
  selection?: ChartSelection; slideInterval?: boolean
  rows: RangeCurve[]; points: PerformancePoint[]; navigationDomain?: number[]; scaleMode: TimeScaleMode; currency: string | null
  rangeTarget?: { finish: () => void; rows: RangeCurve[]; times: number[]; domain: number[]; executions: AnalysisExecutionSummary[] }
}) {
  const plotBounds = useCallback((width: number) => ({ left: 12, right: width - 78, top: 16, bottom: height - 35 }), [height])
  const drag = useRef<{ x: number; y: number; index: number; moved: boolean } | null>(null)
  const readingId = useId(), hintId = useId()
  const times = useMemo(() => points.map(pointTime), [points])
  const scale = useMemo(() => createTimeScale(scaleMode, times), [scaleMode, times])
  const domain = navigationDomain ?? times
  const full = useMemo(() => ({ start: domain[0], end: domain.at(-1)! }), [domain])
  const [viewport, setViewport] = useState(full)
  const symbol = executions?.symbol
  const [hover, setHover] = useState<number | null>(null)
  const [width, setWidth] = useState(0)
  const onResize = useCallback(({ width }: { width: number }) => setWidth(width), [])
  const [selected, setSelected] = useState<ChartSelection>(null)
  const visibleExecutions = executions?.visible && executions.rows.length ? executions.rows : undefined
  const markerBounds = useCallback((width: number) => {
    const plot = plotBounds(width)
    return { ...plot, bottom: plot.bottom + 12 }
  }, [plotBounds])
  const xAtTime = (time: number) => timeX(time, plotBounds(width), viewport, scale)
  const { cursor, magnet, follow, pin, clear: clearExecutionCursor } = useExecutionMarkerInteraction(
    visibleExecutions, viewport, markerBounds(width), xAtTime, !!visibleExecutions, setSelected,
  )
  useEffect(() => {
    if (!visibleExecutions) { setSelected(null); clearExecutionCursor() }
  }, [visibleExecutions, clearExecutionCursor])
  useEffect(() => { setViewport(full); setHover(null); clearExecutionCursor() }, [full, scaleMode, clearExecutionCursor])
  useEffect(() => { setSelected(null); setHover(null); clearExecutionCursor() }, [symbol, clearExecutionCursor])
  const pinned = selected?.kind === 'execution' ? visibleExecutions?.find(row => row.record_id === selected.recordId) : null
  const current = pinned ? nearestIndex(times, pinned.time_ms) : Math.min(hover ?? points.length - 1, points.length - 1)
  const axis = useMemo(() => niceValueAxis(rows.flatMap(row => row.values.filter((value, i): value is number => value != null && (times[i] >= viewport.start && times[i] <= viewport.end || times[i] < viewport.start && (times[i + 1] ?? Infinity) >= viewport.start || times[i] > viewport.end && (times[i - 1] ?? -Infinity) <= viewport.end))), 1), [rows, times, viewport])
  const segmented = rows.some(row => row.baselines.some(base => base != null && base > 0))
  const live = rows.find(row => row.symbol === 'actual')
  const markerPoint = useCallback((row: AnalysisExecutionSummary, width: number, window = viewport, timeScale = scale, anchors = times, series = live) => {
    const time = row.time_ms
    if (time < window.start || time > window.end) return null
    const plot = plotBounds(width)
    const x = timeX(time, plot, window, timeScale)
    const i = nearestIndex(anchors, time), j = anchors[i] < time ? i + 1 : i - 1
    const value = series?.values[i], neighbor = series?.values[j]
    if (value != null && time === anchors[i]) return { x, y: valueY(value, axis, plot) }
    if (!series || value == null || neighbor == null || series.baselines[i] !== series.baselines[j]) return { x, y: plot.bottom + 8 }
    const fraction = (timeScale.project(time) - timeScale.project(anchors[i])) / (timeScale.project(anchors[j]) - timeScale.project(anchors[i]))
    return { x, y: valueY(value + (neighbor - value) * fraction, axis, plot) }
  }, [viewport, plotBounds, scale, times, live, axis])

  const paintAxes = useCallback((ctx: CanvasRenderingContext2D, frame: CanvasFrame, window: Viewport, timeScale: TimeScale, tickDomain?: Viewport) => {
    const plot = plotBounds(frame.width), { theme } = frame
    ctx.font = `11px ${theme.font}`; ctx.lineWidth = 1
    drawValueAxis(ctx, plot, axis, theme, value => Math.abs(value) >= 10000 ? `${(value / 10000).toFixed(1)}万` : amount(value))
    drawTimeAxis(ctx, plot, window, theme, height - 9, (time, intraday) => timeLabel(time).slice(intraday ? 11 : 0, intraday ? 16 : 10), timeScale, undefined, tickDomain)
  }, [axis, plotBounds, height])

  const paintCurves = useCallback((ctx: CanvasRenderingContext2D, frame: CanvasFrame, rows: RangeCurve[], times: number[], window: Viewport, timeScale: TimeScale) => {
    const plot = plotBounds(frame.width), { theme } = frame, css = getComputedStyle(ctx.canvas)
    drawSeries(ctx, plot, times, rows.map(row => {
      const style = seriesStyle(row.symbol)
      return { values: row.values, baselines: row.baselines, color: css.getPropertyValue(style.color.slice(4, -1)).trim(), dash: style.dash, width: 1.8 }
    }), window, axis, timeScale)
    ctx.save(); ctx.beginPath(); ctx.rect(plot.left, plot.top - 3, plot.right - plot.left, plot.bottom - plot.top + 6); ctx.clip()
    rows.forEach(row => row.baselines.forEach((base, i) => {
      if (base !== i || i === 0) return
      ctx.beginPath(); ctx.arc(timeX(times[i], plot, window, timeScale), valueY(0, axis, plot), 3, 0, Math.PI * 2)
      ctx.fillStyle = theme.warn; ctx.fill()
    }))
    ctx.restore()
  }, [axis, plotBounds])

  const drawAxes = useCallback((canvas: HTMLCanvasElement, frame: CanvasFrame) => {
    const ctx = prepareCanvas(canvas, frame.width, frame.height)
    if (ctx) paintAxes(ctx, frame, viewport, scale)
  }, [paintAxes, viewport, scale])
  const drawCurves = useCallback((canvas: HTMLCanvasElement, frame: CanvasFrame) => {
    const ctx = prepareCanvas(canvas, frame.width, frame.height)
    if (ctx) paintCurves(ctx, frame, rows, times, viewport, scale)
  }, [paintCurves, rows, times, viewport, scale])

  const windowMotion = useMemo<WindowMotion | undefined>(() => {
    if (!rangeTarget) return
    const merged = rangeContributionRows(rows, rangeTarget.rows, times, rangeTarget.times)
    const domain = rangeTarget.domain
    const zoom = rangeZoom(viewport, { start: domain[0], end: domain.at(-1)! }, merged.times, scaleMode)
    if (!zoom) return
    const targetLive = merged.rows.find(row => row.symbol === 'actual')
    const markers = executions?.visible ? rangeExecutions(executions.rows, rangeTarget.executions) : null
    return { finish: rangeTarget.finish, draw: (canvas, frame, progress) => {
      const ctx = prepareCanvas(canvas, frame.width, frame.height)
      if (!ctx) return
      const window = zoom.viewport(progress)
      paintAxes(ctx, frame, window, zoom.scale, zoom.domain)
      paintCurves(ctx, frame, merged.rows, merged.times, window, zoom.scale)
      if (markers) drawExecutionMarkers(ctx, markerBounds(frame.width), frame.theme, markers,
        row => markerPoint(row, frame.width, window, zoom.scale, merged.times, targetLive))
    } }
  }, [rangeTarget, rows, times, viewport, scaleMode, executions?.visible, executions?.rows, paintAxes, paintCurves, markerBounds, markerPoint])
  useEffect(() => { if (rangeTarget && !windowMotion) rangeTarget.finish() }, [rangeTarget, windowMotion])

  const drawOverlay = useCallback((canvas: HTMLCanvasElement, frame: CanvasFrame) => {
    const ctx = prepareCanvas(canvas, frame.width, frame.height)
    if (!ctx) return
    const plot = plotBounds(frame.width)
    if (visibleExecutions) drawExecutionMarkers(ctx, markerBounds(frame.width), frame.theme, visibleExecutions, row => markerPoint(row, frame.width))
    const row = pinned ?? visibleExecutions?.find(row => row.record_id === magnet)
    const marker = row ? markerPoint(row, frame.width) : null
    if (pinned) {
      if (marker) drawSelectionGuide(ctx, marker.x, plot.top, plot.bottom, frame.theme.accent)
      drawExecutionRing(ctx, marker, frame.theme, true)
      return
    }
    drawExecutionRing(ctx, marker, frame.theme, false)
    const time = cursor?.time ?? times[current]
    if ((!cursor && hover == null) || time < viewport.start || time > viewport.end) return
    const value = rows.length === 1 ? rows[0].values[current] : null
    const y = cursor ? Math.min(cursor.y, plot.bottom) : value == null ? undefined : valueY(value, axis, plot)
    drawCrosshair(ctx, plot, timeX(time, plot, viewport, scale), frame.theme.muted, y)
  }, [hover, current, rows, times, viewport, scale, axis, plotBounds, pinned, magnet, visibleExecutions, markerBounds, markerPoint, cursor])

  const focusPoint = (index: number) => {
    setHover(index)
    if (times[index] < viewport.start || times[index] > viewport.end) {
      const span = viewport.end - viewport.start
      setViewport(clampViewport({ start: times[index] - span / 2, end: times[index] + span / 2 }, domain))
    }
  }
  const locate = (event: PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect(), plot = plotBounds(rect.width)
    const x = event.clientX - rect.left, y = event.clientY - rect.top
    const time = timeAtX(Math.max(plot.left, Math.min(plot.right, x)), plot, viewport, scale)
    return { x, y, time, index: nearestIndex(times, time) }
  }
  return <div className="min-w-0">
    <div className={heading ? 'flex min-h-36 flex-col justify-center gap-1 @min-[40rem]:min-h-28 @min-[56rem]:min-h-16' : 'mb-1'}>
      {heading}
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs"><span>{segmented ? '分段毛盈亏贡献' : '累计毛盈亏贡献'} · {displayCurrencyUnit(currency) || '按原币种'}</span><span className="text-ink-3">{segmented ? '各段起点归零' : '区间起点归零'}</span></div>
    </div>
    <TimeSeriesChart height={height} label="所选品种毛盈亏贡献曲线，方向键查看日期，异常处分段并从可靠快照重新归零" draw={drawAxes} drawOverlay={drawOverlay} windowMotion={windowMotion} onResize={executions ? onResize : undefined}
      seriesLayer={{ draw: drawCurves, transitionName: executions ? 'contribution-series' : undefined }}
      interval={selection === undefined ? undefined : { selection, animate: slideInterval }}
      navigation={{ times, current, viewport, scale, plot: plotBounds, onFocus: focusPoint,
        onZoom: (factor, anchor = scale.invert((scale.project(viewport.start) + scale.project(viewport.end)) / 2)) => setViewport(view => zoomTimeViewport(view, factor, anchor, domain, scale)),
        onReset: () => setViewport(full) }}
      interaction={{ 'data-testid': 'contribution-chart', 'aria-describedby': `${readingId} ${hintId}`,
        onPointerDown: event => {
          if ((!onInterval && !visibleExecutions) || !event.isPrimary || event.button !== 0) return
          const point = locate(event), plot = markerBounds(width)
          if (point.x < plot.left || point.x > plot.right || point.y < plot.top || point.y > plot.bottom) return
          drag.current = { ...point, moved: false }
          event.currentTarget.focus({ preventScroll: true })
          event.currentTarget.setPointerCapture(event.pointerId)
        },
        onPointerCancel: () => { drag.current = null; clearExecutionCursor() },
        onLostPointerCapture: () => { drag.current = null },
        onPointerUp: event => {
          const start = drag.current
          if (!start) return
          drag.current = null
          const point = locate(event)
          if (!start.moved) { pin(point); return }
          const interval = intervalSelection(times[start.index], times[point.index])
          if (interval?.kind === 'interval') { setSelected(null); onInterval?.(interval.start, interval.end) }
        },
        onPointerMove: event => {
          const point = locate(event), start = drag.current
          if (start && Math.hypot(point.x - start.x, point.y - start.y) > 5) {
            start.moved = true
            clearExecutionCursor()
          }
          setHover(start?.moved ? point.index : nearestIndex(times, follow(point)?.time ?? point.time))
        },
        onPointerLeave: () => { if (!drag.current) { setHover(null); clearExecutionCursor() } },
        onBlur: () => { setHover(null); clearExecutionCursor() },
        onKeyDown: event => {
          if (event.key === 'Escape') { setSelected(null); clearExecutionCursor(); return }
          const time = times[current]
          if ((event.key === 'Enter' || event.key === ' ') && visibleExecutions
            && pin(cursor ?? { x: xAtTime(time), y: plotBounds(width).top, time })) event.preventDefault()
        },
      }}>
      {width > 0 && executions && visibleExecutions && <ChartTradingOverlay width={width} xAtTime={xAtTime} {...executions}
        currency={currency ?? ''} cursor={cursor} selection={selected} onSelect={setSelected} onInspect={clearExecutionCursor} />}
    </TimeSeriesChart>
    <div id={readingId} aria-live="polite" className={`flex flex-wrap gap-x-4 gap-y-2 text-xs tabular-nums ${heading ? 'min-h-7' : ''}`}><span className="text-ink-3">{timeLabel(times[current])}</span>{rows.map(row => {
      const base = row.baselines[current], value = row.values[current]
      return <span key={row.symbol}><SeriesSwatch symbol={row.symbol} /> {row.product} {base != null && base > 0 && '本段盈亏 '}<span className={returnColor(value)}>{value != null && value > 0 ? '+' : ''}{amount(value)}</span>{base != null && base > 0 && <span className="text-ink-3"> · 起点 {timeLabel(times[base]).slice(0, 16)}</span>}</span>
    })}</div>
    {rows.length === 0 && !executions && <p role="status" className="mt-2 text-xs text-ink-3">选择品种查看曲线</p>}
    <p id={hintId} className="sr-only">方向键查看，Home / End 首末点，Ctrl + 滚轮或加减号缩放，双击或 0 恢复范围。{visibleExecutions && '回车查看执行成交，Escape 关闭。'}{rows.flatMap(row => row.baselines.flatMap((base, i) => base === i && i > 0 ? [`${row.product} · ${timeLabel(times[i])} 数据缺口后重新归零。`] : [])).join('')}</p>
  </div>
}
