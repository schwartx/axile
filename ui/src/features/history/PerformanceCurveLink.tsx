import { useLayoutEffect, useRef } from 'react'
import { Link } from '@/components/ui/nav'
import { Sparkline } from '@/components/viz/Sparkline'
import { sparklineCoordinates } from '@/components/viz/sparklineGeometry'
import { registerCurveEndpoint } from '@/features/history/curveTransition'
import { checkPerformance } from '@/features/history/performanceCache'
import type { PerformanceSummary } from '@/types/api'

/** 同一账户累计收益的两个入口共用导航、预取与过渡身份。 */
export function PerformanceCurveLink({ accountId, summary, source, width, height }: {
  accountId: number
  summary?: PerformanceSummary
  source: 'detail' | 'fleet'
  width: number
  height: number
}) {
  const ref = useRef<HTMLAnchorElement>(null)
  const identity = `performance-source-${source}-${accountId}`
  useLayoutEffect(() => {
    const svg = ref.current?.querySelector('svg')
    if (!svg) return
    return registerCurveEndpoint({
      accountId, identity, kind: 'sparkline', element: svg, points: summary?.points ?? [], snapshotId: summary?.version ?? null,
      coordinates: points => sparklineCoordinates(points, width, height),
    })
  }, [accountId, identity, summary, width, height])
  return <Link
    ref={ref}
    to={`/accounts/${accountId}/history`}
    aria-label="查看全部区间累计绩效"
    title="全部区间累计收益"
    className={`group block cursor-pointer rounded focus-visible:outline-2 focus-visible:outline-accent ${source === 'detail' ? '-m-0.5 w-full max-w-[264px] p-0.5 [&_svg]:h-auto [&_svg]:w-full' : '-m-2 p-2'}`}
    onPointerEnter={() => void checkPerformance(accountId, 'all')}
    onFocus={() => void checkPerformance(accountId, 'all')}
    onClick={event => event.stopPropagation()}
  >
    <span className={`${source === 'detail' ? 'block' : 'inline-block'} opacity-70 transition-opacity duration-150 group-hover:opacity-100 group-focus-visible:opacity-100 motion-reduce:transition-none`}>
      <Sparkline data={summary?.points ?? []} width={width} height={height} />
    </span>
  </Link>
}
