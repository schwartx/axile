import { useLayoutEffect, useRef, useState, type ComponentProps, type ReactNode, type RefObject } from 'react'
import { keyboardPoint, readCanvasTheme, timeAtX, type CanvasTheme, type PlotBounds } from '@/components/viz/chartCanvas'
import type { Viewport } from '@/features/history/chartModel'
import type { TimeScale } from '@/features/history/timeScale'

export interface CanvasFrame { width: number; height: number; theme: CanvasTheme }
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
  drawOverlay: (canvas: HTMLCanvasElement, frame: CanvasFrame) => void
  navigation: Navigation
  interaction?: ComponentProps<'canvas'> & { 'data-testid'?: string }
  children?: ReactNode
}

/** 共用画布生命周期和基础导航；业务叠层与选择行为由调用方定制。 */
export function TimeSeriesChart({ height, label, surfaceRef, overlayRef, onResize, draw, drawOverlay, navigation, interaction = {}, children }: Props) {
  const localSurface = useRef<HTMLDivElement>(null), localOverlay = useRef<HTMLCanvasElement>(null)
  const surface = surfaceRef ?? localSurface, overlay = overlayRef ?? localOverlay
  const base = useRef<HTMLCanvasElement>(null)
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
    if (!surface.current || !base.current) return
    draw(base.current, { width: size.width, height, theme: readCanvasTheme(surface.current) })
  }, [draw, height, size, surface])
  useLayoutEffect(() => {
    if (!surface.current || !overlay.current) return
    drawOverlay(overlay.current, { width: size.width, height, theme: readCanvasTheme(surface.current) })
  }, [draw, drawOverlay, height, size, surface, overlay])

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

  return <div ref={surface} className="relative w-full min-w-0" style={{ height }} data-chart="time-series">
    <canvas ref={base} aria-hidden="true" className="absolute inset-0 h-full w-full" />
    <canvas {...interaction} ref={overlay} tabIndex={0} role="group" aria-label={label} className="absolute inset-0 h-full w-full cursor-crosshair touch-pan-y outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
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
    {children}
  </div>
}
