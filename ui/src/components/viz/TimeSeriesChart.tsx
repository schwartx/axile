import { useLayoutEffect, useRef, useState, type ComponentProps, type CSSProperties, type ReactNode, type RefObject } from 'react'
import { keyboardPoint, prepareCanvas, readCanvasTheme, timeAtX, type CanvasTheme, type PlotBounds } from '@/components/viz/chartCanvas'
import { ChartIntervalHighlight } from '@/components/viz/ChartIntervalHighlight'
import type { ChartSelection, Viewport } from '@/features/history/chartModel'
import type { TimeScale } from '@/features/history/timeScale'

export interface CanvasFrame { width: number; height: number; theme: CanvasTheme }
export interface WindowMotion {
  finish: () => void
  draw: (canvas: HTMLCanvasElement, frame: CanvasFrame, progress: number) => void
}
interface Navigation {
  times: number[]
  current: number
  viewport: Viewport
  scale: TimeScale
  plot: (width: number) => PlotBounds
  onFocus: (index: number) => void
  onZoom: (factor: number, anchor?: number) => void
  onReset: () => void
}
interface Props {
  height: number
  label: string
  surfaceRef?: RefObject<HTMLDivElement | null>
  overlayRef?: RefObject<HTMLCanvasElement | null>
  onResize?: (size: { width: number; revision: number }) => void
  draw: (canvas: HTMLCanvasElement, frame: CanvasFrame) => void
  seriesLayer?: { draw: Props['draw']; transitionName?: string }
  windowMotion?: WindowMotion
  drawOverlay: (canvas: HTMLCanvasElement, frame: CanvasFrame) => void
  navigation: Navigation
  interval?: { selection: ChartSelection; animate?: boolean; bottom?: number }
  interaction?: ComponentProps<'canvas'> & { 'data-testid'?: string }
  children?: ReactNode
}

/** 共用画布生命周期和基础导航；业务叠层与选择行为由调用方定制。 */
export function TimeSeriesChart({ height, label, surfaceRef, overlayRef, onResize, draw, seriesLayer, windowMotion, drawOverlay, navigation, interval, interaction = {}, children }: Props) {
  const localSurface = useRef<HTMLDivElement>(null), localOverlay = useRef<HTMLCanvasElement>(null)
  const surface = surfaceRef ?? localSurface, overlay = overlayRef ?? localOverlay
  const base = useRef<HTMLCanvasElement>(null)
  const series = useRef<HTMLCanvasElement>(null)
  const drawSeries = seriesLayer?.draw
  const [size, setSize] = useState({ width: 900, revision: 0 })
  useLayoutEffect(() => {
    const element = surface.current
    if (!element) return
    let revision = 0
    const measure = () => {
      const next = { width: Math.max(240, element.clientWidth), revision: ++revision }
      setSize(next); onResize?.(next)
    }
    measure()
    const resize = new ResizeObserver(measure); resize.observe(element)
    const mutation = new MutationObserver(measure); mutation.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style'] })
    window.addEventListener('resize', measure)
    document.fonts.addEventListener('loadingdone', measure)
    let disposed = false
    void document.fonts.ready.then(() => { if (!disposed) measure() })
    return () => { disposed = true; resize.disconnect(); mutation.disconnect(); window.removeEventListener('resize', measure); document.fonts.removeEventListener('loadingdone', measure) }
  }, [surface, onResize])

  useLayoutEffect(() => {
    if (!surface.current || !base.current || windowMotion) return
    draw(base.current, { width: size.width, height, theme: readCanvasTheme(surface.current) })
  }, [draw, height, size, surface, windowMotion])
  useLayoutEffect(() => {
    if (!windowMotion || !surface.current || !base.current) return
    const canvas = base.current, element = surface.current
    const frame = { width: size.width, height, theme: readCanvasTheme(element) }
    windowMotion.draw(canvas, frame, 0)
    let start: number | undefined
    const paint = (now: number) => {
      start ??= now
      const elapsed = Math.min((now - start) / 240, 1)
      windowMotion.draw(canvas, frame, 1 - (1 - elapsed) ** 3)
      if (elapsed < 1) request = requestAnimationFrame(paint)
      else windowMotion.finish()
    }
    let request = requestAnimationFrame(paint)
    return () => cancelAnimationFrame(request)
  }, [windowMotion, height, size, surface])
  useLayoutEffect(() => {
    if (!surface.current || !series.current) return
    drawSeries?.(series.current, { width: size.width, height, theme: readCanvasTheme(surface.current) })
  }, [drawSeries, height, size, surface])
  useLayoutEffect(() => {
    if (!surface.current || !overlay.current) return
    if (windowMotion) prepareCanvas(overlay.current, size.width, height)
    else drawOverlay(overlay.current, { width: size.width, height, theme: readCanvasTheme(surface.current) })
  }, [draw, drawOverlay, height, size, surface, overlay, windowMotion])

  const wheelZoom = useRef(navigation)
  wheelZoom.current = navigation
  useLayoutEffect(() => {
    const element = overlay.current
    if (!element) return
    const wheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return
      event.preventDefault()
      const nav = wheelZoom.current, width = element.clientWidth
      const x = event.clientX - element.getBoundingClientRect().left
      const anchor = timeAtX(x, nav.plot(width), nav.viewport, nav.scale)
      nav.onZoom(Math.exp(Math.max(-100, Math.min(100, event.deltaY)) * 0.003), anchor)
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [overlay])

  return <div ref={surface} className="relative w-full min-w-0" style={{ height }} data-chart="time-series" data-range-zoom={windowMotion ? '' : undefined} inert={!!windowMotion}>
    <canvas ref={base} aria-hidden="true" className="absolute inset-0 h-full w-full" />
    {seriesLayer && <canvas ref={series} aria-hidden="true" className={`pointer-events-none absolute inset-0 h-full w-full${windowMotion ? ' invisible' : ''}`}
      data-chart-series={seriesLayer.transitionName}
      style={{ '--chart-series-name': seriesLayer.transitionName } as CSSProperties} />}
    {interval && !windowMotion && <ChartIntervalHighlight {...interval} plot={navigation.plot(size.width)} viewport={navigation.viewport}
      scale={navigation.scale} start={navigation.times[0]} end={navigation.times.at(-1)!} />}
    <canvas {...interaction} ref={overlay} data-chart-overlay="" tabIndex={0} role="group" aria-label={label} className="absolute inset-0 h-full w-full cursor-crosshair touch-pan-y outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      onDoubleClick={event => { interaction.onDoubleClick?.(event); if (!event.defaultPrevented) navigation.onReset() }}
      onKeyDown={event => {
        interaction.onKeyDown?.(event)
        if (event.defaultPrevented) return
        const index = keyboardPoint(event.key, navigation.current, navigation.times.length)
        if (index != null) { event.preventDefault(); navigation.onFocus(index) }
        else if (event.key === '+' || event.key === '=') { event.preventDefault(); navigation.onZoom(0.7) }
        else if (event.key === '-') { event.preventDefault(); navigation.onZoom(1.4) }
        else if (event.key === '0') { event.preventDefault(); navigation.onReset() }
      }} />
    {!windowMotion && children}
  </div>
}
