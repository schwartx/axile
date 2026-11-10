import { Info, LoaderCircle, TriangleAlert } from 'lucide-react'
import { Tooltip } from '@/components/ui/Tooltip'
import type { PerformanceSummary } from '@/types/api'

const LABELS = {
  empty: '暂无历史绩效',
  pending: '历史绩效等待更新',
  stale: '历史绩效待更新',
  failed: '历史绩效更新失败',
  ready: '',
}

/** 固定槽位保留收益行布局；仅等待更新时旋转，不把过期结果表现为计算中。 */
export function PerformanceStatus({ status = 'empty' }: { status?: PerformanceSummary['status'] }) {
  const label = LABELS[status]
  const Icon = status === 'failed' ? TriangleAlert : status === 'empty' ? Info : LoaderCircle
  return <span className="ml-1 inline-grid h-4 w-4 shrink-0 place-items-center align-middle">
    {label && <Tooltip content={label}>
      <span tabIndex={0} role="img" aria-label={label}
        className={`inline-flex cursor-default rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${status === 'failed' ? 'text-warn' : 'text-ink-3'}`}>
        <Icon size={13} aria-hidden className={status === 'pending' ? 'animate-spin motion-reduce:animate-none' : undefined} />
      </span>
    </Tooltip>}
  </span>
}
