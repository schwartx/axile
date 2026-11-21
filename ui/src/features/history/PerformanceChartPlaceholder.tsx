import { useLayoutEffect, useRef, useState } from 'react'
import { CHART_HEIGHT, PLOT, plotRight } from '@/components/viz/performanceCanvas'
import { curvePath, sparklineCoordinates } from '@/components/viz/sparklineGeometry'
import { cancelAccountCurveTransition, curvePreviewPoints, registerCurveEndpoint } from '@/features/history/curveTransition'

/** 加载与错误均保留图槽。来源线只用于短暂预览，不提供收益轴、读数或交互。 */
export function PerformanceChartPlaceholder({ accountId, loading, failed, message }: {
  accountId: number; loading: boolean; failed: boolean; message?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  const path = useRef<SVGPathElement>(null)
  const [points] = useState(() => curvePreviewPoints(accountId))
  useLayoutEffect(() => {
    const element = ref.current
    if (!loading || failed) cancelAccountCurveTransition(accountId)
    if (!element || !loading || failed || points.length < 2) return
    const coordinates = () => sparklineCoordinates(points, plotRight(element.clientWidth) - PLOT.left, PLOT.bottom - PLOT.top)
      .map(point => point && ({ x: point.x + PLOT.left, y: point.y + PLOT.top }))
    if (path.current) path.current.setAttribute('d', curvePath(coordinates()))
    const unregister = registerCurveEndpoint({
      accountId, kind: 'chart', identity: `performance-placeholder-${accountId}`, element,
      snapshotId: null, points, coordinates, preview: true,
    })
    // 慢请求不让摘要预览冒充成品，也不无限保留旧线。
    const timer = setTimeout(() => {
      if (!path.current) return
      path.current.style.opacity = '0'
    }, 850)
    return () => { unregister(); clearTimeout(timer) }
  }, [accountId, points, loading, failed])
  return <div data-testid="performance-placeholder">
    <div role="status" className="flex min-h-36 min-w-0 items-center py-1 text-xs text-ink-3 @min-[40rem]:min-h-28 @min-[56rem]:min-h-16">
      {message ?? (failed ? '绩效读取失败' : loading ? '正在准备绩效' : '暂无有效绩效观测')}
    </div>
    <div ref={ref} className="relative w-full min-w-0" style={{ height: CHART_HEIGHT }}>
      {loading && !failed && <svg aria-hidden="true" className="absolute inset-0 h-full w-full"><path ref={path} className="transition-opacity duration-150 motion-reduce:transition-none" fill="none" stroke="var(--color-accent)" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" /></svg>}
    </div>
    <div className="h-7" aria-hidden="true" />
  </div>
}
