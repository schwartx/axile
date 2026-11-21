import { timeX, type PlotBounds } from '@/components/viz/chartCanvas'
import type { ChartSelection, Viewport } from '@/features/history/chartModel'
import type { TimeScale } from '@/features/history/timeScale'
import { MOTION_LAYOUT } from '@/lib/viewTransition'

export function ChartIntervalHighlight({ selection, animate, bottom, plot, viewport, scale, start, end }: {
  selection: ChartSelection
  animate?: boolean
  bottom?: number
  plot: PlotBounds
  viewport: Viewport
  scale: TimeScale
  start: number
  end: number
}) {
  const chosen = selection?.kind === 'interval' ? selection : null
  const left = timeX(chosen?.start ?? start, plot, viewport, scale) - plot.left
  const right = plot.right - timeX(chosen?.end ?? end, plot, viewport, scale)
  const motion = animate ? 'transition-[left,right,opacity] ' + MOTION_LAYOUT : ''
  return <div aria-hidden="true" className="pointer-events-none absolute overflow-hidden"
    style={{ left: plot.left, top: plot.top, width: plot.right - plot.left, height: (bottom ?? plot.bottom) - plot.top }}>
    {/* 拖动或缩放视口时重新投影，不让区间边界追着视口动画。 */}
    <div key={`${viewport.start}:${viewport.end}:${plot.right}:${scale.mode}`} data-testid="chart-interval-highlight"
      className={`absolute inset-y-0 border-x border-accent bg-accent/[0.08] ${chosen ? 'opacity-100' : 'opacity-0'} ${motion}`}
      style={{ left, right }}>
      <span className="absolute -left-[3px] top-0 h-[15px] w-1.5 bg-accent" />
      <span className="absolute -right-[3px] top-0 h-[15px] w-1.5 bg-accent" />
    </div>
  </div>
}
