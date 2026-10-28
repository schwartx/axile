import type { Viewport } from '@/features/history/chartModel'
import { clampViewport } from '@/features/history/chartModel'
import type { TimeScale } from '@/features/history/timeScale'

export interface CanvasTheme { bg: string; surface: string; ink: string; muted: string; line: string; accent: string; warn: string; fill: string; font: string }
export interface PlotBounds { left: number; right: number; top: number; bottom: number }
export interface ValueAxis { min: number; max: number; step: number; ticks: number[] }
export interface ChartSeries {
  values: (number | null | undefined)[]
  color: string
  width?: number
  dash?: number[]
  baselines?: (number | null)[]
}

export function readCanvasTheme(element: HTMLElement): CanvasTheme {
  const css = getComputedStyle(element)
  const color = (name: string) => css.getPropertyValue(`--color-${name}`).trim()
  return { bg: color('bg'), surface: color('surface'), ink: color('ink-2'), muted: color('ink-3'), line: color('line'), accent: color('accent'), warn: color('warn'), fill: color('fill'), font: css.fontFamily }
}

export function prepareCanvas(canvas: HTMLCanvasElement, width: number, height: number): CanvasRenderingContext2D | null {
  const ratio = window.devicePixelRatio || 1
  const w = Math.round(width * ratio), h = Math.round(height * ratio)
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h }
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
  ctx.clearRect(0, 0, width, height)
  return ctx
}

export function timeX(time: number, plot: PlotBounds, view: Viewport, scale?: TimeScale) {
  const project = scale?.project ?? ((value: number) => value)
  return plot.left + (project(time) - project(view.start)) / (project(view.end) - project(view.start) || 1) * (plot.right - plot.left)
}

export function timeAtX(x: number, plot: PlotBounds, view: Viewport, scale?: TimeScale) {
  const project = scale?.project ?? ((value: number) => value)
  const invert = scale?.invert ?? ((value: number) => value)
  return invert(project(view.start) + (x - plot.left) / (plot.right - plot.left || 1) * (project(view.end) - project(view.start)))
}

export const valueY = (value: number, axis: ValueAxis, plot: PlotBounds) => plot.top + (axis.max - value) / (axis.max - axis.min || 1) * (plot.bottom - plot.top)

export function zoomTimeViewport(view: Viewport, factor: number, anchor: number, times: number[], scale: TimeScale) {
  const center = scale.project(anchor)
  return clampViewport({
    start: scale.invert(center + (scale.project(view.start) - center) * factor),
    end: scale.invert(center + (scale.project(view.end) - center) * factor),
  }, times)
}

export function keyboardPoint(key: string, current: number, count: number): number | null {
  if (!count) return null
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  if (key === 'ArrowLeft' || key === 'ArrowRight') return Math.max(0, Math.min(count - 1, current + (key === 'ArrowLeft' ? -1 : 1)))
  return null
}

export function canvasLine(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number, color: string) {
  ctx.strokeStyle = color; ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke()
}

export function drawValueAxis(ctx: CanvasRenderingContext2D, plot: PlotBounds, axis: ValueAxis, theme: CanvasTheme, format: (value: number) => string) {
  for (const value of axis.ticks) {
    const y = valueY(value, axis, plot)
    canvasLine(ctx, plot.left, y, plot.right, y, Math.abs(value) < axis.step * 1e-6 ? theme.muted : theme.line)
    ctx.fillStyle = theme.muted; ctx.fillText(format(value), plot.right + 9, y + 4)
  }
}

export function drawTimeAxis(ctx: CanvasRenderingContext2D, plot: PlotBounds, view: Viewport, theme: CanvasTheme, y: number, format: (time: number, intraday: boolean) => string, scale?: TimeScale, exclude?: { left: number; right: number }) {
  const available = plot.right - plot.left, count = Math.max(1, Math.floor(available / 145))
  const project = scale?.project ?? ((value: number) => value)
  const intraday = project(view.end) - project(view.start) < 864e5
  ctx.fillStyle = theme.muted
  for (let i = 0; i <= count; i++) {
    const x = plot.left + available * i / count
    const label = format(timeAtX(x, plot, view, scale), intraday)
    ctx.textAlign = i === 0 ? 'left' : i === count ? 'right' : 'center'
    const textWidth = ctx.measureText(label).width
    const left = x - (i === 0 ? 0 : i === count ? textWidth : textWidth / 2)
    if (!exclude || left + textWidth < exclude.left || left > exclude.right) ctx.fillText(label, x, y)
  }
  ctx.textAlign = 'left'
}

export function drawSeries(ctx: CanvasRenderingContext2D, plot: PlotBounds, times: number[], series: ChartSeries[], view: Viewport, axis: ValueAxis, scale?: TimeScale, bars = false) {
  ctx.save(); ctx.beginPath(); ctx.rect(plot.left, plot.top, plot.right - plot.left, plot.bottom - plot.top); ctx.clip()
  const count = times.filter(t => t >= view.start && t <= view.end).length
  const barWidth = Math.max(1, Math.min(9, (plot.right - plot.left) / Math.max(2, count) / (series.length + 1)))
  series.forEach((row, index) => {
    ctx.strokeStyle = ctx.fillStyle = row.color
    ctx.lineWidth = row.width ?? 1.5; ctx.setLineDash(row.dash ?? [])
    ctx.beginPath(); let connected = false
    row.values.forEach((value, i) => {
      if (value == null || !Number.isFinite(value)) { connected = false; return }
      const x = timeX(times[i], plot, view, scale), y = valueY(value, axis, plot)
      if (bars) {
        const zero = valueY(0, axis, plot)
        ctx.fillRect(x + (index - series.length / 2) * barWidth, Math.min(zero, y), barWidth, Math.max(1, Math.abs(y - zero)))
      } else {
        if (connected && (!row.baselines || row.baselines[i] === row.baselines[i - 1])) ctx.lineTo(x, y)
        else ctx.moveTo(x, y)
        connected = true
      }
    })
    if (!bars) ctx.stroke()
  })
  ctx.restore()
}

export function drawCrosshair(ctx: CanvasRenderingContext2D, plot: PlotBounds, x: number, color: string, y?: number) {
  ctx.save(); ctx.setLineDash([3, 3])
  canvasLine(ctx, x, plot.top, x, plot.bottom, color)
  if (y != null) canvasLine(ctx, plot.left, y, plot.right, y, color)
  ctx.restore()
}
