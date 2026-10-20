import { Link } from '@/components/ui/nav'
import { performanceJournalPath } from '@/features/account/journalSource'
import { lossClass } from '@/features/history/costs'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { ChartTradingOverlay } from '@/features/history/ChartTradingOverlay'
import { executionSelection } from '@/features/history/executionSelection'
import { executionAt } from '@/features/history/tradeAnnotations'
import { useDomainStore } from '@/stores/domain'
import { useChannelDescriptor } from '@/stores/channels'
import { performanceViewports, type BacktestMode } from '@/features/history/viewState'
import { cancelAccountCurveTransition, registerCurveEndpoint } from '@/features/history/curveTransition'
import { performanceCoordinates } from '@/features/history/curveTransitionGeometry'
import { PerformanceChartPlaceholder } from '@/features/history/PerformanceChartPlaceholder'
import type { AnalysisChart, AnalysisExecutionSummary } from '@/lib/api/performance'
import { Select } from '@/components/ui/Select'
import { OverflowText } from '@/components/ui/OverflowText'
import { chartAxis } from '@/components/viz/performanceCanvas'
import { amount, feeText, shanghaiTime, type CostSummary } from '@/features/history/costs'
import { returnText } from '@/features/history/performance'
import { bindingSelection, clampViewport, intervalReturn, intervalSelection, nearestIndex, navigationTimes as chartNavigationTimes, pointTime, precisePoints, timeLabel, type ChartSelection, type Viewport } from '@/features/history/chartModel'
import { bindingAtObservation, bindingObservationSpan, CHART_HEIGHT, drawOverlay, drawScene, hasExecutionMarker, PLOT, plotRight, pointLabel, readCanvasTheme, seriesKeys, xPosition, xTime, type ChartScene } from '@/components/viz/performanceCanvas'
import { closedDaysBetween, createTimeScale, type TimeScaleMode } from '@/features/history/timeScale'

interface Props {
  accountId: number
  snapshotId?: string | null
  viewKey?: string
  data: AnalysisChart['result']
  executionIndex: AnalysisExecutionSummary[]
  daily: boolean
  backtestMode: BacktestMode
  scaleMode: TimeScaleMode
  costs: Map<string, CostSummary> | null
  intervalCost: (CostSummary & { estimated: number }) | null
  selection: ChartSelection
  onSelect: (selection: ChartSelection) => void
  portfolioNames: Map<number, string>
  controls?: ReactNode
  showExecutions: boolean
}
type DragKind = 'pan' | 'compare' | 'start' | 'end' | 'nav' | 'navStart' | 'navEnd'
interface Drag { kind: DragKind; x: number; y: number; viewport: Viewport; anchor: number; moved: boolean; pending: ChartSelection; bindingTime: number | null }

const slippageCostAmount = (value: number | null) => value == null ? '—' : value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function ClearSelectionButton({ onClick }: { onClick: () => void }) {
  return <button type="button" onClick={onClick} className="inline-flex h-9 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded px-2 text-xs text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent"><X size={16} aria-hidden="true" />取消选择</button>
}

export function PerformanceChart(props: Props) {
  if (props.data.observation_count < 2 || props.data.points.length < 2) return <PerformanceChartPlaceholder accountId={props.accountId} controls={props.controls} loading={false} failed={false} message="有效观测不足两条" />
  return <CanvasPerformanceChart {...props} />
}

function CanvasPerformanceChart({ data, executionIndex, daily, backtestMode, scaleMode, costs, intervalCost, selection, onSelect, portfolioNames, controls, viewKey, accountId, snapshotId, showExecutions }: Props) {
  const accountInfo = useDomainStore(s => s.accounts?.find(a => a.account_id === accountId))
  const descriptor = useChannelDescriptor(accountInfo?.trade_channel)
  const times = useMemo(() => data.points.map(pointTime), [data.points])
  const closedGapDays = useMemo(() => times.slice(0, -1).map((time, index) => closedDaysBetween(time, times[index + 1], data.calendar.closed_ranges)), [times, data.calendar.closed_ranges])
  const timeScale = useMemo(() => createTimeScale(scaleMode, times), [scaleMode, times])
  const navigationTimes = useMemo(() => chartNavigationTimes(data.points, executionIndex), [data.points, executionIndex])
  const full = useMemo(() => ({ start: navigationTimes[0], end: navigationTimes[navigationTimes.length - 1] }), [navigationTimes])
  const [viewport, setViewport] = useState<Viewport>(() => viewKey ? clampViewport(performanceViewports.get(viewKey) ?? full, navigationTimes) : full)
  const lastExecution = useRef(selection?.kind === 'execution' ? selection.recordId : null)
  useEffect(() => { if (viewKey) performanceViewports.set(viewKey, viewport) }, [viewKey, viewport])
  useEffect(() => {
    const id = selection?.kind === 'execution' ? selection.recordId : null
    const changed = id !== lastExecution.current
    lastExecution.current = id
    if (!changed || selection?.kind !== 'execution') return
    setViewport(current => {
      if (selection.time >= current.start && selection.time <= current.end) return current
      const span = current.end - current.start
      return clampViewport({ start: selection.time - span / 2, end: selection.time + span / 2 }, navigationTimes)
    })
  }, [selection, navigationTimes])
  const [returnRange, setReturnRange] = useState<{ min: number; max: number } | null>(null)
  const axisDrag = useRef<{ y: number; min: number; max: number } | null>(null)
  useEffect(() => { setReturnRange(null); axisDrag.current = null }, [daily])
  const [hover, setHover] = useState<number | null>(null)
  const [bindingTime, setBindingTime] = useState<number | null>(null)
  const [draft, setDraft] = useState<ChartSelection>(null)
  const [size, setSize] = useState({ width: 900, revision: 0 })
  const [cursor, setCursor] = useState<{ time: number; x: number; y: number; recordId?: number } | null>(null)
  const [magnet, setMagnet] = useState<number | null>(null)
  const cursorRef = useRef(cursor)
  cursorRef.current = cursor
  const container = useRef<HTMLDivElement>(null)
  const baseCanvas = useRef<HTMLCanvasElement>(null)
  const overlay = useRef<HTMLCanvasElement>(null)
  const tip = useRef<HTMLDivElement>(null)
  const drag = useRef<Drag | null>(null)
  const frame = useRef(0)
  const hoverRef = useRef<number | null>(null)
  const pointer = useRef({ x: 0, y: 0 })
  const magnetRecordId = useRef<number | null>(null)
  const scene = useRef<ChartScene | null>(null)
  const selectionRef = useRef(selection)
  const bindingTimeRef = useRef(bindingTime)
  bindingTimeRef.current = bindingTime
  const exact = precisePoints(data.points)
  const shownSelection = draft ?? selection
  selectionRef.current = shownSelection
  const executionPinned = selection?.kind === 'execution'
  const pointIndex = Math.min(data.points.length - 1, !executionPinned && hover != null ? hover : selection && selection.kind !== 'interval' ? nearestIndex(times, selection.time) : data.points.length - 1)
  const point = data.points[pointIndex]
  const execution = executionIndex.find(row => row.record_id === point.record_id)
  // 悬停读数优先用磁吸命中的执行，与点击固定的取数同源；观测点 record_id 可能绑定到同刻另一条执行。
  const magnetExecution = magnet != null ? executionIndex.find(row => row.record_id === magnet) : null
  const selectedExecution = executionPinned ? executionIndex.find(row => row.record_id === selection.recordId) : null
  const cursorExecution = cursor ? executionAt(executionIndex, cursor.time) : null
  const keys = seriesKeys(daily, backtestMode)
  const summary = draft ? null : intervalCost
  const estimated = summary?.estimated ?? 0
  const daySummary = costs?.get(point.date.slice(0, 10))
  const interval = shownSelection?.kind === 'interval'
  const account = interval ? intervalReturn(data.points, shownSelection, 'account_return') : point[keys[0]]
  const portfolio = interval ? intervalReturn(data.points, shownSelection, backtestMode === 'target' ? 'target_portfolio_return' : 'portfolio_return') : point[keys[1]]
  const readingExecution = selectedExecution ?? magnetExecution ?? (cursor ? cursorExecution : execution)
  const readingCost = interval ? summary : selection?.kind === 'day' ? daySummary : readingExecution?.summary ?? daySummary
  const costReady = costs != null && (!interval || summary != null)
  const costScope = interval ? '区间' : readingExecution ? '本次执行' : '当日'
  const costIncomplete = readingCost != null && readingCost.count > 0 && (readingCost.covered < readingCost.count || !readingCost.amountComplete)
  const coverage = !costReady ? '成交额覆盖 —（加载中）'
    : !readingCost || readingCost.count === 0 ? '成交额覆盖 —（无成交）'
    : readingCost.coverage == null ? '成交额覆盖 —（数据不完整）'
    : `${(readingCost.coverage * 100).toFixed(1)}% 成交额覆盖`
  const coverageNeedsAttention = costReady && readingCost != null && readingCost.count > 0 && (costIncomplete || readingCost.coverage == null)
  const binding = bindingAtObservation(data, times, bindingTime ?? times[pointIndex])
  const bindingName = !binding || binding.binding.portfolio_id == null ? '未绑定' : portfolioNames.get(binding.binding.portfolio_id) ?? `组合 #${binding.binding.portfolio_id}`
  const tradingScene = useMemo<ChartScene>(() => ({ data, executionIndex, times, width: size.width, viewport, daily, backtestMode, returnRange, costs, portfolioNames, domain: full, scale: timeScale, showExecutions, closedGapDays, theme: container.current ? readCanvasTheme(container.current) : { bg: '', surface: '', ink: '', muted: '', line: '', accent: '', warn: '', fill: '', font: '' } }), [data, executionIndex, times, size, viewport, daily, backtestMode, returnRange, costs, portfolioNames, full, timeScale, showExecutions, closedGapDays])

  const paintOverlay = useCallback(() => {
    if (overlay.current && scene.current) drawOverlay(overlay.current, scene.current, hoverRef.current, selectionRef.current, bindingTimeRef.current, cursorRef.current, magnet)
  }, [magnet])
  const schedule = useCallback(() => {
    if (frame.current) return
    frame.current = requestAnimationFrame(() => {
      frame.current = 0; paintOverlay()
      if (tip.current) {
        const w = tip.current.offsetWidth, h = tip.current.offsetHeight
        const x = pointer.current.x + w + 24 > size.width ? pointer.current.x - w - 18 : pointer.current.x + 18
        tip.current.style.left = `${Math.max(8, Math.min(size.width - w - 8, x))}px`
        tip.current.style.top = `${Math.max(8, Math.min(PLOT.costBottom - h, pointer.current.y - h / 2))}px`
      }
    })
  }, [paintOverlay, size.width])

  useLayoutEffect(() => {
    const element = container.current
    if (!element) return
    const measure = () => setSize(s => ({ width: Math.max(240, element.clientWidth), revision: s.revision + 1 }))
    measure()
    const resize = new ResizeObserver(measure); resize.observe(element)
    const mutation = new MutationObserver(measure); mutation.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style'] })
    window.addEventListener('resize', measure)
    document.fonts.addEventListener('loadingdone', measure)
    let disposed = false
    void document.fonts.ready.then(() => { if (!disposed) measure() })
    return () => { disposed = true; resize.disconnect(); mutation.disconnect(); window.removeEventListener('resize', measure); document.fonts.removeEventListener('loadingdone', measure); cancelAnimationFrame(frame.current); frame.current = 0 }
  }, [])

  useEffect(() => {
    setViewport(view => clampViewport(view, navigationTimes))
    setHover(null); hoverRef.current = null; setDraft(null); drag.current = null
    setCursor(null); setBindingTime(null); magnetRecordId.current = null; setMagnet(null)
  }, [data.points, times, navigationTimes, executionIndex])
  useEffect(() => { setHover(null); hoverRef.current = null; setDraft(null); drag.current = null; setCursor(null); magnetRecordId.current = null; setMagnet(null) }, [scaleMode])

  useLayoutEffect(() => {
    if (!container.current || !baseCanvas.current) return
    scene.current = { ...tradingScene, theme: readCanvasTheme(container.current) }
    drawScene(baseCanvas.current, scene.current); paintOverlay()
  }, [tradingScene, paintOverlay])
  useLayoutEffect(() => {
    const element = container.current
    const canReturn = !daily && scaleMode === 'observations' && viewKey === `${accountId}:all` && viewport.start === full.start && viewport.end === full.end && returnRange == null
    if (!canReturn) cancelAccountCurveTransition(accountId)
    if (!element || daily) return
    return registerCurveEndpoint({
      accountId, kind: 'chart', identity: `performance-chart-${accountId}`, element, points: data.points,
      snapshotId: snapshotId ?? null,
      coordinates: points => performanceCoordinates(tradingScene, points),
      canReturn,
    })
  }, [accountId, data.points, daily, scaleMode, snapshotId, tradingScene, viewKey, viewport, full, returnRange])
  useLayoutEffect(() => { paintOverlay(); schedule() }, [shownSelection, hover, bindingTime, cursor, paintOverlay, schedule])

  const changeView = (view: Viewport) => setViewport(clampViewport(view, navigationTimes))
  const zoom = (factor: number, anchor = timeScale.invert((timeScale.project(viewport.start) + timeScale.project(viewport.end)) / 2)) => setViewport(view => {
    const center = timeScale.project(anchor)
    return clampViewport({
      start: timeScale.invert(center + (timeScale.project(view.start) - center) * factor),
      end: timeScale.invert(center + (timeScale.project(view.end) - center) * factor),
    }, navigationTimes)
  })
  useEffect(() => {
    const element = overlay.current
    if (!element) return
    const wheel = (event: WheelEvent) => {
      if (!event.ctrlKey || !scene.current) return
      event.preventDefault()
      const current = scene.current
      const scale = current.scale ?? timeScale
      const x = event.clientX - element.getBoundingClientRect().left
      const delta = Math.max(-100, Math.min(100, event.deltaY))
      const anchor = xTime(x, current.width, current.viewport, scale)
      const factor = Math.exp(delta * 0.003), center = scale.project(anchor)
      setViewport(view => clampViewport({
        start: scale.invert(center + (scale.project(view.start) - center) * factor),
        end: scale.invert(center + (scale.project(view.end) - center) * factor),
      }, navigationTimes))
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [navigationTimes, timeScale])

  const locate = (event: PointerEvent<HTMLElement>) => {
    const rect = container.current!.getBoundingClientRect()
    const x = event.clientX - rect.left, y = event.clientY - rect.top
    const time = xTime(Math.max(PLOT.left, Math.min(plotRight(size.width), x)), size.width, viewport, timeScale)
    return { x, y, time, index: nearestIndex(times, time) }
  }
  /**
   * 执行点以时间轴为磁场，而不挪动点本身；这样吸附手感不会伪造执行时刻。
   * 已吸附的点使用更宽的退出阈值，避免相邻点之间来回抖动。
   */
  const magneticExecution = (p: { x: number; y: number }) => {
    if (!showExecutions) { magnetRecordId.current = null; setMagnet(null); return null }
    if (p.y < PLOT.top || p.y > PLOT.bottom) { magnetRecordId.current = null; setMagnet(null); return null }
    const visible = executionIndex.flatMap(row => {
      const time = row.time_ms
      if (!hasExecutionMarker(row) || time < viewport.start || time > viewport.end) return []
      return [{ row, time, distance: Math.abs(xPosition(time, size.width, viewport, timeScale) - p.x) }]
    })
    const retained = visible.find(item => item.row.record_id === magnetRecordId.current)
    if (retained && retained.distance <= 18) { setMagnet(retained.row.record_id); return retained }
    const nearest = visible.reduce<typeof visible[number] | null>((best, item) => !best || item.distance < best.distance ? item : best, null)
    if (!nearest || nearest.distance > 12) { magnetRecordId.current = null; setMagnet(null); return null }
    magnetRecordId.current = nearest.row.record_id
    setMagnet(nearest.row.record_id)
    return nearest
  }
  const showHover = (index: number | null) => { hoverRef.current = index; setHover(previous => previous === index ? previous : index); schedule() }
  const startDrag = (event: PointerEvent<HTMLElement>) => {
    if (!event.isPrimary || event.button !== 0) return
    const p = locate(event)
    if (p.x < PLOT.left || p.x > plotRight(size.width) || p.y < PLOT.top) return
    if ((p.y > PLOT.costBottom && p.y < PLOT.navTop) || p.y > PLOT.navBottom) return
    const onBinding = p.y >= PLOT.binding && p.y <= PLOT.binding + PLOT.bindingHeight
    setBindingTime(onBinding ? p.time : null)
    overlay.current?.focus({ preventScroll: true }); event.currentTarget.setPointerCapture(event.pointerId)
    let kind: DragKind = !onBinding && exact ? 'compare' : 'pan'
    let anchor = times[p.index]
    if (p.y >= PLOT.navTop) {
      const left = xPosition(viewport.start, size.width, full, timeScale), right = xPosition(viewport.end, size.width, full, timeScale)
      kind = Math.abs(p.x - left) < 14 ? 'navStart' : Math.abs(p.x - right) < 14 ? 'navEnd' : 'nav'
    } else if (!onBinding && selection?.kind === 'interval') {
      if (Math.abs(p.x - xPosition(selection.start, size.width, viewport, timeScale)) < 10) { kind = 'start'; anchor = selection.end }
      else if (Math.abs(p.x - xPosition(selection.end, size.width, viewport, timeScale)) < 10) { kind = 'end'; anchor = selection.start }
    }
    drag.current = { kind, x: p.x, y: p.y, viewport, anchor, moved: false, pending: selection, bindingTime: onBinding ? p.time : null }
    pointer.current = p; showHover(p.index)
  }
  const move = (event: PointerEvent<HTMLElement>) => {
    const p = locate(event); pointer.current = p
    const active = drag.current
    if (!active) {
      const inside = p.x >= PLOT.left && p.x <= plotRight(size.width) && p.y >= PLOT.top && p.y <= PLOT.costBottom
      const snapped = inside ? magneticExecution(p) : null
      const time = snapped?.time ?? p.time
      setCursor(inside && p.y <= PLOT.bottom ? { x: p.x, y: p.y, time, recordId: snapped?.row.record_id } : null)
      showHover(inside ? nearestIndex(times, time) : null)
      setBindingTime(inside && p.y >= PLOT.binding && p.y <= PLOT.binding + PLOT.bindingHeight ? p.time : null)
      return
    }
    if (Math.hypot(p.x - active.x, p.y - active.y) > 5) active.moved = true
    if (!active.moved) return
    setCursor(null)
    const nav = active.kind.startsWith('nav')
    if (active.kind === 'pan' || active.kind === 'nav') {
      const scale = nav ? full : active.viewport
      const delta = (xTime(p.x, size.width, scale, timeScale) - xTime(active.x, size.width, scale, timeScale)) * (nav ? 1 : -1)
      changeView({ start: active.viewport.start + delta, end: active.viewport.end + delta })
    } else if (nav) {
      const time = xTime(p.x, size.width, full, timeScale)
      changeView(active.kind === 'navStart' ? { start: Math.min(time, active.viewport.end - 1), end: active.viewport.end } : { start: active.viewport.start, end: Math.max(time, active.viewport.start + 1) })
    } else {
      active.pending = intervalSelection(active.anchor, times[p.index]); setDraft(active.pending)
    }
    showHover(p.index)
  }
  const selectPoint = (index: number) => {
    const time = times[index]
    const row = executionIndex.find(row => row.record_id === data.points[index].record_id)
    onSelect(row ? executionSelection(row) : selection?.kind === 'day' && selection.time === time ? null : { kind: 'day', day: data.points[index].date.slice(0, 10), time })
  }
  const finish = (event: PointerEvent<HTMLElement>) => {
    const active = drag.current
    if (!active) return
    const p = locate(event)
    drag.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    if (active.kind.startsWith('nav')) {
      if (!active.moved && active.kind === 'nav') {
        const center = xTime(p.x, size.width, full, timeScale), half = (viewport.end - viewport.start) / 2
        changeView({ start: center - half, end: center + half })
      }
    } else if (active.moved) {
      if (active.kind !== 'pan' && active.pending) onSelect(active.pending)
    } else if (active.bindingTime != null) {
      const period = bindingAtObservation(data, times, active.bindingTime)
      if (exact && period) {
        const next = bindingSelection(times, shanghaiTime(period.binding.time), shanghaiTime(period.end), period.binding === data.bindings.at(-1))
        if (next) onSelect(next)
      }
    } else if (p.y >= PLOT.costTop && p.y <= PLOT.costBottom) onSelect({ kind: 'day', day: data.points[p.index].date.slice(0, 10), time: times[p.index] })
    else {
      const magnet = showExecutions ? magneticExecution(p) : null
      const row = magnet?.row ?? executionAt(executionIndex, p.time)
      if (row) onSelect(executionSelection(row))
      else selectPoint(p.index)
    }
    setDraft(null)
  }
  const focusPoint = (index: number) => {
    const time = times[index]
    if (time < viewport.start || time > viewport.end) {
      const span = viewport.end - viewport.start
      changeView(time < viewport.start ? { start: time, end: time + span } : { start: time - span, end: time })
    }
    showHover(index)
  }
  const clear = () => { drag.current = null; setDraft(null); onSelect(null); showHover(null); setCursor(null) }

  return <div data-testid="performance-workbench" className="min-w-0">
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
    <div className="flex min-h-9 min-w-0 flex-1 basis-[32rem] flex-col gap-y-1 py-1 text-xs text-ink-2" data-testid="chart-readout">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="break-words text-ink-3">{interval ? '区间比较' : `日末观测 ${pointLabel(point)}`}</span>
        <span>账户 <b className="font-medium text-accent">{returnText(account)}</b></span>
        <span>回测 <b className="font-medium">{returnText(portfolio)}</b></span>
        {backtestMode === 'sized' && !interval && point.sizing_fallback && <span className="text-ink-3">本次按目标权重回退</span>}
        <span>收益差 <b className="font-medium">{returnText(account != null && portfolio != null ? portfolio - account : null, ' 个百分点')}</b></span>
      </div>
      <div className="flex min-h-5 flex-wrap items-center gap-x-3 gap-y-1 text-ink-3 tabular-nums">
        <span>{costScope}成交额 {amount(costReady ? readingCost?.value ?? null : null)}</span>
        <span className={lossClass(costReady ? readingCost?.lossBp : null)}>滑点损耗 {amount(costReady ? readingCost?.lossBp ?? null : null)} BP</span>
        <span className={lossClass(costReady ? readingCost?.cost : null)}>滑点成本 {slippageCostAmount(costReady ? readingCost?.cost ?? null : null)} {accountInfo?.currency ?? ''}</span>
        <span className={coverageNeedsAttention ? 'text-warn' : undefined}>{coverage}</span>
        {interval && costReady && readingCost && <><span>手续费 {feeText(readingCost)}</span>{estimated > 0 && <span className="text-warn">{estimated} 笔使用执行时间</span>}</>}
      </div>
    </div>
    <div className="flex flex-wrap items-center gap-2">
      {shownSelection && !interval && <ClearSelectionButton onClick={clear} />}
      {snapshotId && <Link aria-disabled={!!draft} tabIndex={draft ? -1 : undefined} onClick={event => { if (draft) event.preventDefault() }} data-testid="journal-link" to={performanceJournalPath(accountId, snapshotId, data.range, selection)} className="inline-flex min-h-9 items-center text-xs text-accent hover:underline">{selection?.kind === 'interval' ? '查看区间执行' : selection?.kind === 'day' ? '查看当日执行' : selection?.kind === 'execution' ? '查看本次执行' : '查看执行记录'}</Link>}
      {controls}
    </div>
    </div>
    <div inert={!interval} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${interval ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden">
      <div className="mt-1 flex min-h-9 flex-wrap items-center gap-2 border-b border-line pb-1 text-xs text-ink-2">
      {shownSelection?.kind === 'interval' && <>
        {(['start', 'end'] as const).map(edge => <label key={edge} className="flex min-w-0 items-center gap-2"><span>{edge === 'start' ? '起点' : '终点'}</span><Select
          ariaLabel={edge === 'start' ? '比较起点' : '比较终点'}
          className="h-9 w-[210px] max-w-full min-w-0 justify-between px-2 text-xs"
          value={shownSelection[edge]}
          options={data.points.map((p, i) => ({ value: times[i], label: pointLabel(p) })).filter(option => option.value !== shownSelection[edge === 'start' ? 'end' : 'start'])}
          onChange={value => onSelect(intervalSelection(value, shownSelection[edge === 'start' ? 'end' : 'start']))}
        /></label>)}
        <ClearSelectionButton onClick={clear} />
      </>}
      </div>
      </div>
    </div>
    <div ref={container} className="relative w-full min-w-0" style={{ height: CHART_HEIGHT }}>
      <canvas ref={baseCanvas} aria-hidden="true" className="absolute inset-0 h-full w-full" />
      <canvas ref={overlay} data-testid="performance-chart" tabIndex={0} role="group" aria-label={daily ? '每日收益与执行成本' : '累计收益与执行成本'} aria-describedby="performance-keyboard-reading performance-interaction-hint" className="absolute inset-0 h-full w-full cursor-crosshair touch-pan-y outline-none"
        onPointerDown={startDrag} onPointerMove={move} onPointerUp={finish} onPointerCancel={() => { drag.current = null; setDraft(null); showHover(null) }} onLostPointerCapture={() => { drag.current = null; setDraft(null) }}
        onPointerLeave={event => { if (!drag.current) { magnetRecordId.current = null; setMagnet(null); showHover(null); setCursor(null); if (event.pointerType !== 'touch') setBindingTime(null) } }} onBlur={() => { if (!drag.current) showHover(null) }} onDoubleClick={() => setViewport(full)}
        onKeyDown={event => {
          const index = hoverRef.current ?? pointIndex
          if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); focusPoint(Math.max(0, Math.min(times.length - 1, index + (event.key === 'ArrowLeft' ? -1 : 1)))) }
          else if (event.key === 'Home' || event.key === 'End') { event.preventDefault(); focusPoint(event.key === 'Home' ? 0 : times.length - 1) }
          else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); if (exact && selection?.kind === 'day') onSelect(intervalSelection(selection.time, times[index]) ?? selection); else selectPoint(index) }
          else if (event.key === 'Escape') { event.preventDefault(); clear() }
          else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(0.7) }
          else if (event.key === '-') { event.preventDefault(); zoom(1.4) }
          else if (event.key === '0') { event.preventDefault(); setViewport(full); setReturnRange(null) }
        }} />
      <div className="pointer-events-none absolute inset-0"
        onPointerDown={startDrag} onPointerMove={move} onPointerUp={finish}
        onPointerCancel={() => { drag.current = null; setDraft(null); showHover(null) }}
        onLostPointerCapture={() => { drag.current = null; setDraft(null) }}
        onPointerLeave={event => { if (!drag.current) { magnetRecordId.current = null; setMagnet(null); showHover(null); setCursor(null); if (event.pointerType !== 'touch') setBindingTime(null) } }}
        onDoubleClick={() => setViewport(full)}>
        {data.bindings.map((period, index) => {
          const span = bindingObservationSpan(data, times, index)
          if (!span) return null
          const start = Math.max(PLOT.left, xPosition(span.start, size.width, viewport, timeScale))
          const end = Math.min(plotRight(size.width), xPosition(span.end, size.width, viewport, timeScale))
          if (end - start < 90) return null
          const name = period.portfolio_id == null ? '未绑定' : portfolioNames.get(period.portfolio_id) ?? `组合 #${period.portfolio_id}`
          return <div key={`${period.time}-${index}`} className="pointer-events-auto absolute cursor-crosshair touch-pan-y px-2 text-[11px] leading-6 text-ink-3"
            style={{ left: start, top: PLOT.binding, width: end - start, height: PLOT.bindingHeight }}>
            <OverflowText text={name} />
          </div>
        })}
      </div>
      <div role="group" aria-label="收益率轴缩放" data-testid="return-axis" className="absolute right-0 cursor-ns-resize touch-none"
        style={{ top: PLOT.top, height: PLOT.bottom - PLOT.top, width: PLOT.right }}
        onPointerDown={event => {
          if (!event.isPrimary || event.button !== 0 || !scene.current) return
          axisDrag.current = { y: event.clientY, ...chartAxis(scene.current) }
          showHover(null); setBindingTime(null)
          event.currentTarget.setPointerCapture(event.pointerId)
        }}
        onPointerMove={event => {
          const active = axisDrag.current
          if (!active) return
          const factor = Math.exp(Math.max(-7, Math.min(7, (event.clientY - active.y) * 0.01)))
          const center = (active.min + active.max) / 2
          const half = Math.max(1e-8, Math.min(1e8, (active.max - active.min) * factor / 2))
          setReturnRange({ min: center - half, max: center + half })
        }}
        onPointerUp={event => { axisDrag.current = null; if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId) }}
        onPointerCancel={() => { axisDrag.current = null }} onLostPointerCapture={() => { axisDrag.current = null }}
        onDoubleClick={() => setReturnRange(null)} />
      <div ref={tip} role="tooltip" className={`pointer-events-none absolute z-10 hidden w-[260px] max-w-[calc(100%-16px)] rounded border border-line bg-surface px-3 py-2 text-xs shadow-sm ${hover != null && !drag.current && !cursor && selection?.kind !== 'execution' ? 'block' : ''}`}>
        {bindingTime != null && binding ? <>
          <div className="break-words font-medium">{bindingName}</div>
          <div className="mt-2 text-ink-3">{binding.binding.time.replace('T', ' ')}<br />→ {binding.end.replace('T', ' ')}</div>
          <div className="mt-2 text-ink-2">持续 {Number(((shanghaiTime(binding.end) - shanghaiTime(binding.binding.time)) / 864e5).toFixed(1))} 天</div>
          {(!exact || !bindingSelection(times, shanghaiTime(binding.binding.time), shanghaiTime(binding.end), binding.binding === data.bindings.at(-1))) && <div className="mt-2 text-ink-3">有效观测不足，无法比较</div>}
        </> : <>
        <div className="mb-2 text-ink-3">{pointLabel(point)}</div>
        <div className="mb-1 text-ink-3">日末收益观测</div>
        <div className="flex justify-between gap-4"><span className="text-accent">账户</span><span>{returnText(point[keys[0]])}</span></div>
        <div className="flex justify-between gap-4"><span>回测</span><span>{returnText(point[keys[1]])}</span></div>
        {backtestMode === 'sized' && point.sizing_fallback && <div className="text-ink-3">本次按目标权重回退</div>}
        <div className="flex justify-between gap-4"><span>当日滑点成本</span><span>{slippageCostAmount(daySummary?.cost ?? null)}</span></div>
        <div className="mt-2 break-words border-t border-line pt-2">{bindingName}</div>
        {binding && <div className="mt-1 text-[11px] text-ink-3">{binding.binding.time.replace('T', ' ')}<br />→ {binding.end?.replace('T', ' ') ?? '当前'}</div>}
        </>}
      </div>
      {snapshotId && <ChartTradingOverlay scene={tradingScene} rows={executionIndex} snapshotId={snapshotId} accountId={accountId} currency={accountInfo?.currency ?? ''} units={descriptor?.units} cursor={cursor} selection={selection} onSelect={onSelect} onInspect={setCursor} />}
    </div>

    <p id="performance-keyboard-reading" className="sr-only" aria-live="polite">{pointLabel(point)}，账户 {returnText(point[keys[0]])}，回测 {returnText(point[keys[1]])}。方向键选点，回车确认，Escape 清除，加减号缩放，0 恢复范围。</p>
    {scaleMode === 'observations' && <p className="sr-only">观测序列已压缩 {closedGapDays.reduce((sum, days) => sum + days, 0)} 个完整休市日。</p>}
    <div className="flex min-h-7 flex-wrap justify-between gap-2 text-[11px] text-ink-3"><span data-testid="chart-viewport">{timeLabel(viewport.start)} → {timeLabel(viewport.end)}</span><span>上海时间 · 收益差 = 回测 − 账户</span></div>
    <p className="text-[11px] leading-5 text-ink-3">账户收益未调整出入金 · 回测单边费率 {Number((data.settings.backtest_fee_rate * 10000).toFixed(8))} BP</p>
    <div id="performance-interaction-hint" className="sr-only">
      <span>Ctrl + 滚轮缩放</span><span>拖动框选区间 · 底部导航条平移 · 双击恢复时间范围</span><span>聚焦图表后：← → 查看 · Home / End 首末点 · Enter / 空格选择 · Esc 清除 · + / - 缩放 · 0 重置</span>
    </div>
  </div>
}
