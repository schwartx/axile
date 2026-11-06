import type { TargetMode } from '@/types/api'
import { Check } from 'lucide-react'
import { WeightBars } from '@/components/viz/WeightBars'
import { formatTargetWeight, targetDirectionClass, targetWeightSummary } from './portfolioCardSummary'

/** 试跑通过的返回权重清单；结果带 / panel 已提供分层，内容不套卡片壳。 */
export function WeightResult({ target, mode = 'weight', quantityUnit = '' }: {
  target: Record<string, number>; mode?: TargetMode; quantityUnit?: string
}) {
  const summary = targetWeightSummary(target, mode)
  const entries = summary.entries.map(({ symbol, weight }) => [symbol, weight] as [string, number])
  return (
    <div>
      <div className="mb-2.5 flex flex-wrap items-center justify-between gap-x-5 gap-y-1.5 text-[14px]">
        <span className="flex items-center gap-1.5 font-[550] text-accent"><Check size={14} /> {mode === 'quantity' ? '目标持仓' : '返回权重'}</span>
        <dl className="flex flex-wrap items-baseline justify-end gap-x-4 gap-y-1 text-ink-3">
          <div className="flex items-baseline gap-1.5">
            <dt className="sr-only">目标品种数</dt>
            <dd><span className="num text-ink-2">{summary.activeCount}</span> 个目标品种</dd>
          </div>
          {mode === 'weight' && <><div className="flex items-baseline gap-1.5">
            <dt>净敞口</dt>
            <dd className={`num ${targetDirectionClass(summary.netExposure)}`}>
              {formatTargetWeight(summary.netExposure)}
            </dd>
          </div>
          <div className="flex items-baseline gap-1.5">
            <dt>总敞口</dt>
            <dd className="num text-ink-2">{formatTargetWeight(summary.grossExposure, false)}</dd>
          </div></>}
        </dl>
      </div>
      {entries.length === 0 ? (
        <p className="text-[14px] text-ink-3">返回空仓 {'{}'}</p>
      ) : (
        <WeightBars weights={entries} format={mode === 'quantity' ? 'quantity' : 'raw'} quantityUnit={quantityUnit} />
      )}
    </div>
  )
}
