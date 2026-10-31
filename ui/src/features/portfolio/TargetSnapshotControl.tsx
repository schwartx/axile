import { shanghaiLabel } from '@/features/history/costs'
import type { ErrorEvidence } from '@/lib/errorInfo'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { RefreshCw } from 'lucide-react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Tooltip } from '@/components/ui/Tooltip'
import { targetSnapshotText } from '@/features/portfolio/targetSnapshotDisplay'
import type { TargetWeightSnapshot } from '@/types/api'

export function TargetSnapshotControl({
  snapshot,
  loading = false,
  recalculating,
  error,
  readError = null,
  disabled = false,
  disabledReason,
  contextName,
  variant = 'full',
  contextKey,
  evidence = [],
  onRecalculate,
}: {
  snapshot: TargetWeightSnapshot | null
  loading?: boolean
  recalculating: boolean
  error: Error | null
  readError?: Error | null
  disabled?: boolean
  disabledReason?: string
  contextName?: string | null
  variant?: 'full' | 'compact'
  contextKey?: string | number
  evidence?: ErrorEvidence[]
  onRecalculate: () => void
}) {
  const tooltip = disabledReason ?? (recalculating ? '正在重新计算目标权重' : '重新计算当前目标权重')
  const meta = loading && !snapshot
    ? '正在读取目标权重…'
    : readError && !snapshot ? '目标暂不可用' : targetSnapshotText(snapshot, contextName)
  const compactMeta = loading && !snapshot
    ? '目标读取中'
    : snapshot?.calculated_at
      ? `目标计算于 ${shanghaiLabel(snapshot.calculated_at).slice(11, 16)}`
      : readError ? '目标暂不可用' : '目标未计算'

  const refreshButton = (
    <Tooltip content={tooltip}>
      <span className="inline-flex flex-none">
        <button
          type="button"
          onClick={onRecalculate}
          disabled={disabled || loading || recalculating}
          aria-label={recalculating ? '正在重新计算目标权重' : '重新计算当前目标权重'}
          className="grid h-6 w-6 cursor-pointer place-items-center rounded-md text-ink-3 hover:bg-fill hover:text-ink-1 disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent"
        >
          <RefreshCw size={14} className={recalculating ? 'animate-spin motion-reduce:animate-none' : undefined} />
        </button>
      </span>
    </Tooltip>
  )

  if (variant === 'compact') {
    return (
      <div className="min-w-0"><div className="flex min-w-0 items-center gap-0.5">
        <Tooltip content={meta}>
          <span className="min-w-0 text-[13px] text-ink-3">
            <InkRewrite text={compactMeta} tone="label" />
          </span>
        </Tooltip>
        {refreshButton}
      </div><ErrorNotice intent="mutation" evidence={evidence} contextKey={contextKey} title="目标计算失败" error={error} variant="compact" onRetry={onRecalculate} /></div>
    )
  }

  return (
    <div>
      <div className="flex min-w-0 items-center justify-between gap-3">
        <div title={meta} className="min-w-0 text-[13.5px] text-ink-3">
          <InkRewrite text={meta} tone="label" />
        </div>
        {refreshButton}
      </div>
      <ErrorNotice intent="mutation" evidence={evidence} contextKey={contextKey} title="目标计算失败" error={error} variant={snapshot?.calculated_at ? 'stale' : 'section'}
        updatedAt={snapshot?.calculated_at ? Date.parse(snapshot.calculated_at) : null} onRetry={onRecalculate} />
    </div>
  )
}
