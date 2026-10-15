import { WeightBars } from '@/components/viz/WeightBars'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { SkeletonLines } from '@/components/ui/Skeleton'
import type { useTargetSnapshot } from '@/lib/hooks/useTargetSnapshot'
import { formatTargetWeight, targetDirectionClass, targetWeightSummary } from './portfolioCardSummary'

type TargetSnapshotState = ReturnType<typeof useTargetSnapshot>

/**
 * 「目标」面板的生效 tab 正文：持久目标快照（账户下次调仓实际使用的权重）。
 * 排版与试跑结果（WeightResult）同构（摘要行 + WeightBars），来源由面板头部的
 * 生效/试跑分段显式命名；快照特有的加载 / 错误 / 空态在这里收口。
 */
export function SnapshotResult({ weights, contextKey }: { weights: TargetSnapshotState; contextKey?: string | number }) {
  const summary = targetWeightSummary(weights.data?.weights ?? {})
  const entries = summary.entries.map(({ symbol, weight }) => [symbol, weight] as [string, number])

  return (
    <div>
      {!weights.data && weights.loading && <SkeletonLines rows={3} />}
      <ErrorNotice hasData={weights.data != null} intent="read" contextKey={contextKey}
        title="目标权重快照加载失败"
        error={weights.error}
        variant={weights.stale ? 'stale' : 'section'}
        updatedAt={weights.updatedAt}
        onRetry={weights.reloadSnapshot}
      />
      <ErrorNotice intent="mutation" contextKey={contextKey}
        title="重新计算失败"
        error={weights.recalculateError}
        variant="mutation"
        onRetry={() => void weights.recalculate()}
      />
      {!weights.loading && !weights.error && !weights.data?.calculated_at && (
        <p className="text-[13.5px] text-ink-3">尚无目标权重，点击右上角刷新按钮计算。</p>
      )}
      {!weights.loading && !weights.error && weights.data?.calculated_at && entries.length === 0 && (
        <p className="text-[13.5px] text-ink-3">当前目标为空仓。</p>
      )}
      {entries.length > 0 && (
        <>
          <div className="mb-2.5 flex flex-wrap items-center justify-end gap-x-5 gap-y-1.5 text-[14px]">
            <dl className="flex flex-wrap items-baseline justify-end gap-x-4 gap-y-1 text-ink-3">
              <div className="flex items-baseline gap-1.5">
                <dt>品种数量</dt>
                <dd className="num text-ink-2">{summary.activeCount}</dd>
              </div>
              <div className="flex items-baseline gap-1.5">
                <dt>净敞口</dt>
                <dd className={`num ${targetDirectionClass(summary.netExposure)}`}>
                  {formatTargetWeight(summary.netExposure)}
                </dd>
              </div>
              <div className="flex items-baseline gap-1.5">
                <dt>总敞口</dt>
                <dd className="num text-ink-2">{formatTargetWeight(summary.grossExposure, false)}</dd>
              </div>
            </dl>
          </div>
          <WeightBars weights={entries} format="raw" />
        </>
      )}
    </div>
  )
}
