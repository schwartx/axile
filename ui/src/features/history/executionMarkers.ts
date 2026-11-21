import { useCallback, useRef, useState } from 'react'
import { canvasLine, type CanvasTheme, type PlotBounds } from '@/components/viz/chartCanvas'
import type { AnalysisExecutionSummary, CostExecutionRow } from '@/lib/api/performance'
import type { ChartSelection, Viewport } from './chartModel'
import { executionAt, executionSelection } from './executionSelection'

export type ExecutionMarkerTone = 'normal' | 'warn'

/**
 * 执行针脚只保留两态：失败或成交覆盖不全走琥珀实心点，其余一律中性空心圈。
 *
 * 红绿只属于收益曲线本身；有利/不利的质量细节由成本面板三态承载，
 * 针脚不复述，颜色预算留给「偏离」。
 */
export function executionMarkerTone(row: CostExecutionRow | AnalysisExecutionSummary): ExecutionMarkerTone {
  const { summary } = row
  const success = 'record_id' in row ? row.is_success : row.record.is_success
  if (success !== 1 || (!row.noop && (summary.coverage == null || summary.covered < summary.count))) return 'warn'
  return 'normal'
}

/** 有真实成交才画点，部分成交也算；无成交的执行不留针脚、不参与磁吸。 */
export function hasExecutionMarker(row: CostExecutionRow | AnalysisExecutionSummary): boolean {
  return 'record_id' in row ? row.has_transactions : (row.transactions?.length ?? 0) > 0
}

type MarkerPoint = { x: number; y: number }
export type ExecutionCursor = MarkerPoint & { time: number; recordId?: number }

export function drawExecutionMarkers(ctx: CanvasRenderingContext2D, plot: PlotBounds, theme: CanvasTheme,
  rows: AnalysisExecutionSummary[], pointFor: (row: AnalysisExecutionSummary) => MarkerPoint | null) {
  if (!rows.length) return
  ctx.save(); ctx.beginPath(); ctx.rect(plot.left, plot.top, plot.right - plot.left, plot.bottom - plot.top); ctx.clip()
  for (const row of rows) {
    if (!hasExecutionMarker(row)) continue
    const point = pointFor(row)
    if (!point) continue
    const warn = executionMarkerTone(row) === 'warn'
    ctx.beginPath(); ctx.arc(point.x, point.y, warn ? 1.9 : 2.1, 0, Math.PI * 2)
    if (warn) { ctx.fillStyle = theme.warn; ctx.fill() }
    else { ctx.fillStyle = theme.bg; ctx.fill(); ctx.strokeStyle = theme.muted; ctx.stroke() }
  }
  ctx.restore(); ctx.lineWidth = 1
}

export function drawExecutionRing(ctx: CanvasRenderingContext2D, point: MarkerPoint | null, theme: CanvasTheme, pinned: boolean) {
  if (!point) return
  ctx.save(); ctx.strokeStyle = theme.accent; ctx.lineWidth = 1.25; ctx.globalAlpha = pinned ? 0.75 : 0.55
  ctx.beginPath(); ctx.arc(point.x, point.y, pinned ? 7 : 5.5, 0, Math.PI * 2); ctx.stroke()
  ctx.restore()
}

export function drawSelectionGuide(ctx: CanvasRenderingContext2D, x: number, top: number, bottom: number, color: string) {
  canvasLine(ctx, x, top, x, bottom, color)
  ctx.fillStyle = color; ctx.fillRect(x - 3, top, 6, 15)
}

/** 已吸附的执行点使用更宽的退出阈值，避免在相邻点之间来回抖动。 */
export function snapExecution(rows: AnalysisExecutionSummary[], viewport: Viewport, xAtTime: (time: number) => number,
  x: number, retainedId: number | null): AnalysisExecutionSummary | null {
  let nearest: AnalysisExecutionSummary | null = null
  let nearestDistance = 12
  for (const row of rows) {
    if (!hasExecutionMarker(row) || row.time_ms < viewport.start || row.time_ms > viewport.end) continue
    const distance = Math.abs(xAtTime(row.time_ms) - x)
    if (row.record_id === retainedId && distance <= 18) return row
    if (distance <= nearestDistance && (!nearest || distance < nearestDistance)) {
      nearest = row
      nearestDistance = distance
    }
  }
  return nearest
}

export function useExecutionMarkerInteraction(rows: AnalysisExecutionSummary[] | undefined, viewport: Viewport, plot: PlotBounds,
  xAtTime: (time: number) => number, showExecutions: boolean, onSelect: (selection: ChartSelection) => void) {
  const [cursor, setCursor] = useState<ExecutionCursor | null>(null)
  const retained = useRef<number | null>(null)
  const clear = useCallback(() => { retained.current = null; setCursor(null) }, [])
  const inside = (point: MarkerPoint) => point.x >= plot.left && point.x <= plot.right && point.y >= plot.top && point.y <= plot.bottom
  const snap = (point: MarkerPoint) => {
    const row = rows && showExecutions && inside(point) ? snapExecution(rows, viewport, xAtTime, point.x, retained.current) : null
    retained.current = row?.record_id ?? null
    return row
  }
  const follow = (point: ExecutionCursor) => {
    if (!rows || !inside(point)) { clear(); return null }
    const row = snap(point)
    const next = { ...point, time: row?.time_ms ?? point.time, recordId: row?.record_id }
    setCursor(next)
    return next
  }
  const pin = (point: ExecutionCursor) => {
    const row = snap(point) ?? (rows && executionAt(rows, point.time))
    if (!row) return false
    onSelect(executionSelection(row))
    return true
  }
  return { cursor, magnet: showExecutions ? cursor?.recordId ?? null : null, follow, pin, clear }
}
