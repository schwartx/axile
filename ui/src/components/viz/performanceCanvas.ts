import { canvasLine as line, drawCrosshair, drawSeries, drawTimeAxis as drawSharedTimeAxis, drawValueAxis, prepareCanvas, timeAtX, timeX, valueY, type CanvasTheme } from '@/components/viz/chartCanvas'
export { readCanvasTheme, type CanvasTheme } from '@/components/viz/chartCanvas'
import type { PerformancePoint } from '@/types/api'
import type { AnalysisChart, AnalysisExecutionSummary, CostExecutionRow } from '@/lib/api/performance'
import { axisPercent, niceReturnAxis } from '@/features/history/performance'
import { amount, shanghaiTime, type CostSummary } from '@/features/history/costs'
import { pointTime, timeLabel, type ChartSelection, type Viewport } from '@/features/history/chartModel'
import type { TimeScale } from '@/features/history/timeScale'
import type { BacktestMode } from '@/features/history/viewState'
import { closedDaysBetween, closedRangeLabel } from '@/features/history/timeScale'

export const CHART_HEIGHT = 600
export const PLOT = { left: 12, right: 78, top: 16, bottom: 379, binding: 408, bindingHeight: 24, costTop: 464, costBottom: 504, navTop: 548, navBottom: 582 }
const CALENDAR_MARK_Y = PLOT.costBottom + 12
const TIME_AXIS_Y = PLOT.costBottom + 31
export type SeriesKey = 'account_return' | 'portfolio_return' | 'target_portfolio_return' | 'account_daily_return' | 'portfolio_daily_return' | 'target_portfolio_daily_return'
export interface ChartScene {
  domain?: Viewport
  data: AnalysisChart['result']
  executionIndex?: AnalysisExecutionSummary[]
  times: number[]
  width: number
  viewport: Viewport
  daily: boolean
  backtestMode?: BacktestMode
  returnRange?: { min: number; max: number } | null
  costs: Map<string, CostSummary> | null
  portfolioNames: Map<number, string>
  theme: CanvasTheme
  scale?: TimeScale
  /** 是否绘制执行事件针脚；缺省绘制。关闭后磁吸交互由组件侧一并失效。 */
  showExecutions?: boolean
  /** 相邻观测间完整休市天数缓存（len = times.length - 1）；缺省时退回逐帧现算。 */
  closedGapDays?: number[]
}

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

export function executionMarkerPoint(scene: ChartScene, row: CostExecutionRow | AnalysisExecutionSummary): { x: number; y: number } | null {
  const time = 'record_id' in row ? row.time_ms : shanghaiTime(row.record.created_at)
  const recordId = 'record_id' in row ? row.record_id : row.record.id
  const { times, data, width, viewport } = scene
  if (time < viewport.start || time > viewport.end) return null
  const key = seriesKeys(scene.daily, scene.backtestMode)[0]
  const exact = data.points.findIndex(point => point.record_id === recordId)
  let before = exact, after = exact
  if (exact < 0) {
    // 早于首观测/晚于末观测的执行吸附到最近观测点取值，x 仍按真实时间投影。
    after = times.findIndex(value => value >= time)
    if (after < 0) after = times.length - 1
    before = Math.max(0, after - 1)
  }
  if (before < 0 || after < 0) return null
  const a = data.points[before][key], b = data.points[after][key]
  if (a == null || b == null || !Number.isFinite(a) || !Number.isFinite(b)) return null
  const fraction = before === after ? 0 : (time - times[before]) / (times[after] - times[before] || 1)
  const value = a + (b - a) * Math.max(0, Math.min(1, fraction))
  return { x: xPosition(time, width, viewport, scene.scale), y: returnY(value, chartAxis(scene)) }
}

export const seriesKeys = (daily: boolean, mode: BacktestMode = 'sized'): [SeriesKey, SeriesKey] => daily
  ? ['account_daily_return', mode === 'target' ? 'target_portfolio_daily_return' : 'portfolio_daily_return']
  : ['account_return', mode === 'target' ? 'target_portfolio_return' : 'portfolio_return']
export const plotRight = (width: number) => width - PLOT.right
export const plotBounds = (width: number) => ({ left: PLOT.left, right: plotRight(width), top: PLOT.top, bottom: PLOT.bottom })
export const xPosition = (time: number, width: number, view: Viewport, scale?: TimeScale) => timeX(time, plotBounds(width), view, scale)
export const xTime = (x: number, width: number, view: Viewport, scale?: TimeScale) => timeAtX(x, plotBounds(width), view, scale)

export function chartAxis(scene: ChartScene) {
  if (scene.returnRange) {
    const { min, max } = scene.returnRange
    const rough = (max - min) / 5
    const power = 10 ** Math.floor(Math.log10(rough))
    const step = ([1, 2, 2.5, 5, 10].find(n => n * power >= rough) ?? 10) * power
    const first = Math.ceil(min / step) * step
    const ticks = Array.from({ length: Math.max(0, Math.floor((max - first) / step) + 1) }, (_, i) => first + i * step)
    return { min, max, step, ticks }
  }
  const keys = seriesKeys(scene.daily, scene.backtestMode)
  const values = scene.data.points.flatMap((p, i) => scene.times[i] >= scene.viewport.start && scene.times[i] <= scene.viewport.end || !scene.daily && (scene.times[i] < scene.viewport.start && (scene.times[i + 1] ?? Infinity) >= scene.viewport.start || scene.times[i] > scene.viewport.end && (scene.times[i - 1] ?? -Infinity) <= scene.viewport.end)
    ? keys.flatMap(key => typeof p[key] === 'number' && Number.isFinite(p[key]) ? [p[key]!] : []) : [])
  return niceReturnAxis(values)
}

export function returnY(value: number, axis: ReturnType<typeof niceReturnAxis>) {
  return valueY(value, axis, PLOT)
}

function textFit(ctx: CanvasRenderingContext2D, text: string, width: number): string {
  if (ctx.measureText(text).width <= width) return text
  let end = text.length
  while (end > 0 && ctx.measureText(`${text.slice(0, end)}…`).width > width) end--
  return end ? `${text.slice(0, end)}…` : ''
}

function drawBindings(ctx: CanvasRenderingContext2D, scene: ChartScene) {
  const { data, width, viewport, theme, times } = scene
  ctx.fillStyle = theme.muted; ctx.fillText('组合绑定', PLOT.left, PLOT.binding - 12)
  data.bindings.forEach((_, i) => {
    const span = bindingObservationSpan(data, times, i)
    if (!span) return
    const start = Math.max(PLOT.left, xPosition(span.start, width, viewport, scene.scale))
    const end = Math.min(plotRight(width), xPosition(span.end, width, viewport, scene.scale))
    if (end <= start) return
    line(ctx, start, PLOT.binding + PLOT.bindingHeight - 1, end, PLOT.binding + PLOT.bindingHeight - 1, theme.line)
    const boundary = xPosition(span.start, width, viewport, scene.scale)
    if (boundary >= PLOT.left) line(ctx, boundary, PLOT.binding + 4, boundary, PLOT.binding + PLOT.bindingHeight - 1, theme.muted)
  })
}

/** 将绑定区间映射到首个受其影响的收益观测点。 */
export function bindingObservationSpan(data: AnalysisChart['result'], times: number[], index: number): { start: number; end: number } | null {
  const binding = data.bindings[index]
  if (!binding || !times.length) return null
  const start = times.find(time => time >= shanghaiTime(binding.time))
  if (start == null) return null
  const next = data.bindings[index + 1]
  const end = next ? (times.find(time => time >= shanghaiTime(next.time)) ?? times.at(-1)) : times.at(-1)
  if (end == null || end <= start) return null
  return { start, end }
}

function drawReturns(ctx: CanvasRenderingContext2D, scene: ChartScene) {
  const { data, width, viewport, theme, times } = scene
  const axis = chartAxis(scene), plot = plotBounds(width)
  drawValueAxis(ctx, plot, axis, theme, value => textFit(ctx, axisPercent(value, axis.step), PLOT.right - 12))
  drawSeries(ctx, plot, times, seriesKeys(scene.daily, scene.backtestMode).map((key, index) => ({
    values: data.points.map(point => point[key]), color: index === 0 ? theme.accent : theme.ink,
    width: index === 0 ? 1.8 : 1.5, dash: index === 1 ? [5, 3] : [],
  })), viewport, axis, scene.scale, scene.daily)
}

function drawExecutionMarkers(ctx: CanvasRenderingContext2D, scene: ChartScene) {
  const rows = scene.executionIndex ?? []
  if (!rows.length) return
  const { theme } = scene
  ctx.save(); ctx.beginPath(); ctx.rect(PLOT.left, PLOT.top, plotRight(scene.width) - PLOT.left, PLOT.bottom - PLOT.top); ctx.clip()
  for (const row of rows) {
    if (!hasExecutionMarker(row)) continue
    const point = executionMarkerPoint(scene, row)
    if (!point) continue
    // 常态是挖空的中性圈，曲线从圈心穿过；琥珀实心点只留给异常执行。
    const warn = executionMarkerTone(row) === 'warn'
    ctx.beginPath(); ctx.arc(point.x, point.y, warn ? 1.9 : 2.1, 0, Math.PI * 2)
    if (warn) { ctx.fillStyle = theme.warn; ctx.fill() }
    else { ctx.fillStyle = theme.bg; ctx.fill(); ctx.strokeStyle = theme.muted; ctx.stroke() }
  }
  ctx.restore(); ctx.lineWidth = 1
}

function drawCosts(ctx: CanvasRenderingContext2D, scene: ChartScene) {
  const { data, times, costs, width, viewport, theme } = scene
  ctx.fillStyle = theme.muted; ctx.fillText('每日滑点成本', PLOT.left, PLOT.costTop - 10)
  const days = data.points.flatMap((p, i) => (i === data.points.length - 1 || p.date.slice(0, 10) !== data.points[i + 1].date.slice(0, 10)) && times[i] >= viewport.start && times[i] <= viewport.end ? [{ p, time: times[i] }] : [])
  const values = days.flatMap(({ p }) => { const v = costs?.get(p.date.slice(0, 10))?.cost; return v == null ? [] : [v] })
  if (!values.length) {
    const missing = days.some(({ p }) => costs?.has(p.date.slice(0, 10)))
    ctx.fillText(costs == null ? '成本数据未就绪' : missing ? '成本缺失' : '本区间无成交记录', PLOT.left, (PLOT.costTop + PLOT.costBottom) / 2)
    return
  }
  const low = Math.min(0, ...values), high = Math.max(0, ...values)
  const y = (v: number) => high === low ? (PLOT.costTop + PLOT.costBottom) / 2 : PLOT.costTop + (high - v) / (high - low) * (PLOT.costBottom - PLOT.costTop)
  line(ctx, PLOT.left, y(0), plotRight(width), y(0), theme.line)
  ctx.fillStyle = theme.muted
  for (const value of new Set([low, high])) ctx.fillText(textFit(ctx, amount(value), PLOT.right - 12), plotRight(width) + 9, y(value) + 4)
  const w = Math.max(1, Math.min(16, (plotRight(width) - PLOT.left) / Math.max(2, days.length) * 0.6))
  ctx.save(); ctx.beginPath(); ctx.rect(PLOT.left, PLOT.costTop - 1, plotRight(width) - PLOT.left, PLOT.costBottom - PLOT.costTop + 3); ctx.clip()
  days.forEach(({ p, time }) => {
    const summary = costs?.get(p.date.slice(0, 10)), value = summary?.cost
    const x = xPosition(time, width, viewport, scene.scale)
    if (value == null) { ctx.fillStyle = theme.muted; ctx.fillText('·', x - 2, PLOT.costBottom); return }
    ctx.globalAlpha = summary!.covered < summary!.count ? 0.5 : 0.9
    ctx.fillStyle = value > 0 ? theme.warn : value < 0 ? theme.accent : theme.muted
    ctx.fillRect(x - w / 2, Math.min(y(0), y(value)), w, Math.max(1, Math.abs(y(value) - y(0))))
  })
  ctx.restore()
}

function drawTimeAxis(ctx: CanvasRenderingContext2D, scene: ChartScene, exclude?: { left: number; right: number }) {
  drawSharedTimeAxis(ctx, plotBounds(scene.width), scene.viewport, scene.theme, TIME_AXIS_Y,
    (time, intraday) => timeLabel(time).slice(intraday ? 11 : 0, intraday ? 16 : 10), scene.scale, exclude)
}

function drawNavigator(ctx: CanvasRenderingContext2D, scene: ChartScene) {
  const { width, viewport, times, data, theme } = scene
  const full = scene.domain ?? { start: times[0], end: times[times.length - 1] }
  ctx.fillStyle = theme.line; ctx.fillRect(PLOT.left, PLOT.navTop, plotRight(width) - PLOT.left, PLOT.navBottom - PLOT.navTop)
  if (scene.scale?.mode === 'natural') {
    ctx.fillStyle = theme.fill
    for (const range of data.calendar.closed_ranges) {
      const start = new Date(`${range.start}T00:00:00+08:00`).getTime()
      const end = new Date(`${range.end}T00:00:00+08:00`).getTime() + 86_400_000
      const left = Math.max(PLOT.left, xPosition(start, width, full, scene.scale))
      const right = Math.min(plotRight(width), xPosition(end, width, full, scene.scale))
      if (right > left) ctx.fillRect(left, PLOT.navTop, right - left, PLOT.navBottom - PLOT.navTop)
    }
  }
  const values = data.points.flatMap(p => p.account_return == null ? [] : [p.account_return])
  const low = Math.min(0, ...values), high = Math.max(0.01, ...values)
  ctx.strokeStyle = theme.muted; ctx.beginPath(); let connected = false
  data.points.forEach((p, i) => {
    if (p.account_return == null) { connected = false; return }
    const x = xPosition(times[i], width, full, scene.scale), y = PLOT.navBottom - 4 - (p.account_return - low) / (high - low) * 25
    if (connected) ctx.lineTo(x, y); else ctx.moveTo(x, y)
    connected = true
  })
  ctx.stroke()
  const a = xPosition(viewport.start, width, full, scene.scale), b = xPosition(viewport.end, width, full, scene.scale)
  ctx.fillStyle = theme.bg; ctx.globalAlpha = 0.65
  ctx.fillRect(PLOT.left, PLOT.navTop, a - PLOT.left, PLOT.navBottom - PLOT.navTop)
  ctx.fillRect(b, PLOT.navTop, plotRight(width) - b, PLOT.navBottom - PLOT.navTop)
  ctx.globalAlpha = 1; ctx.strokeStyle = theme.accent; ctx.strokeRect(a, PLOT.navTop, b - a, PLOT.navBottom - PLOT.navTop)
  for (const x of [a, b]) { ctx.fillStyle = theme.accent; ctx.fillRect(x - 2, PLOT.navTop + 8, 4, 18) }
}

export function drawScene(canvas: HTMLCanvasElement, scene: ChartScene) {
  const ctx = prepareCanvas(canvas, scene.width, CHART_HEIGHT)
  if (!ctx) return
  ctx.font = `11px ${scene.theme.font}`
  drawCalendar(ctx, scene); drawReturns(ctx, scene); if (scene.showExecutions !== false) drawExecutionMarkers(ctx, scene); drawBindings(ctx, scene); drawCosts(ctx, scene); drawTimeAxis(ctx, scene); drawNavigator(ctx, scene); drawCalendarMarks(ctx, scene)
}

function drawCalendar(ctx: CanvasRenderingContext2D, scene: ChartScene) {
  if (scene.scale?.mode !== 'natural') return
  ctx.save(); ctx.fillStyle = scene.theme.fill; ctx.globalAlpha = 0.65
  for (const range of scene.data.calendar.closed_ranges) {
    const start = new Date(`${range.start}T00:00:00+08:00`).getTime()
    const end = new Date(`${range.end}T00:00:00+08:00`).getTime() + 86_400_000
    const left = Math.max(PLOT.left, xPosition(start, scene.width, scene.viewport, scene.scale))
    const right = Math.min(plotRight(scene.width), xPosition(end, scene.width, scene.viewport, scene.scale))
    if (right <= left) continue
    for (const [top, bottom] of [[PLOT.top, PLOT.bottom], [PLOT.binding, PLOT.binding + PLOT.bindingHeight], [PLOT.costTop, PLOT.costBottom], [PLOT.navTop, PLOT.navBottom]]) ctx.fillRect(left, top, right - left, bottom - top)
    if (right - left >= 42) { ctx.globalAlpha = 1; ctx.fillStyle = scene.theme.muted; ctx.fillText('休市', left + 7, PLOT.top + 15); ctx.fillStyle = scene.theme.fill; ctx.globalAlpha = 0.65 }
  }
  ctx.restore()
}

/** measureText 结果按「字体 + 文本」缓存；休市标签逐帧重画但文本集合有限。 */
const textWidths = new Map<string, number>()
function measuredWidth(ctx: CanvasRenderingContext2D, text: string): number {
  const key = `${ctx.font} ${text}`
  let width = textWidths.get(key)
  if (width == null) {
    width = ctx.measureText(text).width
    textWidths.set(key, width)
  }
  return width
}

function drawCalendarMarks(ctx: CanvasRenderingContext2D, scene: ChartScene) {
  if (scene.scale?.mode !== 'observations') return
  for (let index = 0; index < scene.times.length - 1; index++) {
    const days = scene.closedGapDays?.[index] ?? closedDaysBetween(scene.times[index], scene.times[index + 1], scene.data.calendar.closed_ranges)
    if (!days) continue
    const left = xPosition(scene.times[index], scene.width, scene.viewport, scene.scale)
    const right = xPosition(scene.times[index + 1], scene.width, scene.viewport, scene.scale)
    const visibleLeft = Math.max(PLOT.left, left), visibleRight = Math.min(plotRight(scene.width), right)
    const available = visibleRight - visibleLeft
    if (available <= 4) continue
    ctx.fillStyle = scene.theme.fill; ctx.globalAlpha = 0.55
    ctx.fillRect(visibleLeft + 2, CALENDAR_MARK_Y - 10, Math.max(0, available - 4), 14)
    ctx.globalAlpha = 1; ctx.fillStyle = scene.theme.muted; ctx.textAlign = 'center'
    const full = `休市 ${days} 日`
    const label = closedRangeLabel(available, days, measuredWidth(ctx, full), measuredWidth(ctx, '休市'))
    if (label) ctx.fillText(label, (visibleLeft + visibleRight) / 2, CALENDAR_MARK_Y)
  }
  ctx.textAlign = 'left'
}

export function drawOverlay(canvas: HTMLCanvasElement, scene: ChartScene, hover: number | null, selection: ChartSelection, bindingTime: number | null = null, cursor: { time: number; x: number; y: number } | null = null, magnetRecordId: number | null = null) {
  const ctx = prepareCanvas(canvas, scene.width, CHART_HEIGHT)
  if (!ctx) return
  const { width, viewport, theme, times, data } = scene
  const right = plotRight(width)
  ctx.font = `11px ${theme.font}`
  const binding = bindingTime == null ? null : bindingAtObservation(data, times, bindingTime)
  if (binding) {
    const index = data.bindings.indexOf(binding.binding)
    const span = bindingObservationSpan(data, times, index)
    if (span) {
      const a = Math.max(PLOT.left, xPosition(span.start, width, viewport, scene.scale))
      const b = Math.min(right, xPosition(span.end, width, viewport, scene.scale))
      ctx.fillStyle = theme.accent; ctx.globalAlpha = 0.1
      ctx.fillRect(a, PLOT.binding, Math.max(0, b - a), PLOT.bindingHeight)
      ctx.globalAlpha = 1
      line(ctx, a, PLOT.binding + PLOT.bindingHeight - 1, b, PLOT.binding + PLOT.bindingHeight - 1, theme.accent)
    }
  }
  if (magnetRecordId != null && selection?.kind !== 'execution') {
    const row = scene.executionIndex?.find(value => value.record_id === magnetRecordId)
    if (row) {
      const point = executionMarkerPoint(scene, row)
      if (point) {
        // 吸附/选中是交互反馈，统一走 accent，不继承执行点自身颜色。
        ctx.strokeStyle = theme.accent; ctx.lineWidth = 1.25; ctx.globalAlpha = 0.55
        ctx.beginPath(); ctx.arc(point.x, point.y, 5.5, 0, Math.PI * 2); ctx.stroke()
        ctx.globalAlpha = 1; ctx.lineWidth = 1
      }
    }
  }
  if (selection) {
    const a = xPosition(selection.kind !== 'interval' ? selection.time : selection.start, width, viewport, scene.scale)
    const b = selection.kind === 'interval' ? xPosition(selection.end, width, viewport, scene.scale) : a
    ctx.save(); ctx.beginPath(); ctx.rect(PLOT.left, PLOT.top, right - PLOT.left, PLOT.costBottom - PLOT.top); ctx.clip()
    if (selection.kind === 'interval') { ctx.fillStyle = theme.accent; ctx.globalAlpha = 0.08; ctx.fillRect(a, PLOT.top, b - a, PLOT.costBottom - PLOT.top); ctx.globalAlpha = 1 }
    for (const x of new Set([a, b])) {
      line(ctx, x, PLOT.top, x, PLOT.costBottom, theme.accent)
      ctx.fillStyle = theme.accent; ctx.fillRect(x - 3, PLOT.top, 6, 15)
    }
    ctx.restore()
    if (selection.kind === 'execution') {
      const row = scene.executionIndex?.find(value => value.record_id === selection.recordId)
      if (row) {
        const point = executionMarkerPoint(scene, row)
        if (point) {
          ctx.strokeStyle = theme.accent; ctx.lineWidth = 1.25; ctx.globalAlpha = 0.75
          ctx.beginPath(); ctx.arc(point.x, point.y, 7, 0, Math.PI * 2); ctx.stroke()
          ctx.globalAlpha = 1; ctx.lineWidth = 1
        }
      }
    }
  }
  if (cursor && selection?.kind !== 'execution') {
    const x = xPosition(cursor.time, width, viewport, scene.scale)
    drawCrosshair(ctx, { ...plotBounds(width), bottom: PLOT.costBottom }, x, theme.muted, cursor.y)
    const axis = chartAxis(scene)
    const value = axis.max - (cursor.y - PLOT.top) / (PLOT.bottom - PLOT.top) * (axis.max - axis.min)
    ctx.fillStyle = theme.bg; ctx.fillRect(right + 2, cursor.y - 10, PLOT.right - 2, 20)
    ctx.fillStyle = theme.ink; ctx.fillText(axisPercent(value, axis.step), right + 8, cursor.y + 4)
    const label = timeLabel(cursor.time), labelWidth = ctx.measureText(label).width + 12
    const labelX = Math.max(PLOT.left, Math.min(right - labelWidth, x - labelWidth / 2))
    ctx.fillStyle = theme.ink; ctx.fillRect(labelX, PLOT.costBottom + 18, labelWidth, 22)
    ctx.fillStyle = theme.bg; ctx.fillText(label, labelX + 6, TIME_AXIS_Y + 2)
    return
  }
  if (hover == null || !data.points[hover] || selection?.kind === 'execution') return
  const x = xPosition(times[hover], width, viewport, scene.scale)
  if (x < PLOT.left || x > right) return
  drawCrosshair(ctx, { ...plotBounds(width), bottom: PLOT.costBottom }, x, theme.muted)
  const axis = chartAxis(scene)
  const marks = seriesKeys(scene.daily, scene.backtestMode).flatMap((key, series) => {
    const value = data.points[hover][key]
    return value == null ? [] : [{ value, series, y: returnY(value, axis) }]
  }).sort((a, b) => a.y - b.y)
  marks.forEach(({ series, y }) => {
    const color = series === 0 ? theme.accent : theme.ink
    ctx.fillStyle = theme.bg; ctx.strokeStyle = color; ctx.lineWidth = 2
    ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); ctx.lineWidth = 1
  })
  const label = timeLabel(times[hover])
  const labelWidth = ctx.measureText(label).width + 12
  const labelX = Math.max(PLOT.left, Math.min(right - labelWidth, x - labelWidth / 2))
  ctx.fillStyle = theme.bg; ctx.fillRect(PLOT.left, PLOT.costBottom + 18, right - PLOT.left, PLOT.navTop - PLOT.costBottom - 20)
  drawTimeAxis(ctx, scene, { left: labelX - 8, right: labelX + labelWidth + 8 })
  ctx.fillStyle = theme.ink; ctx.fillRect(labelX, PLOT.costBottom + 18, labelWidth, 22)
  ctx.fillStyle = theme.bg; ctx.fillText(label, labelX + 6, TIME_AXIS_Y + 2)
}

export function bindingAt(data: AnalysisChart['result'], time: number) {
  const index = data.bindings.findLastIndex(b => shanghaiTime(b.time) <= time)
  return index < 0 ? null : { binding: data.bindings[index], end: data.bindings[index + 1]?.time ?? data.end }
}

/** 绑定行按最近一次已发生的收益观测来解释鼠标位置。 */
export function bindingAtObservation(data: AnalysisChart['result'], times: number[], time: number) {
  const observed = times.findLast(value => value <= time)
  return observed == null ? null : bindingAt(data, observed)
}

export function pointLabel(point: PerformancePoint): string {
  return point.observed_at ? timeLabel(pointTime(point)) : point.date.replace('T', ' ')
}
