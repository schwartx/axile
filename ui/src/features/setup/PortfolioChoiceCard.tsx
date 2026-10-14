import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useCallback } from 'react'
import { Code2 } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { TargetChips } from '@/features/portfolio/TargetChips'
import { formatTargetUpdatedAt, formatTargetWeight, portfolioTargetState, targetDirectionClass } from '@/features/portfolio/portfolioCardSummary'
import { getPortfolioTargetSnapshot } from '@/lib/api/portfolios'
import { usePolling } from '@/lib/hooks/usePolling'
import type { PortfolioLite } from '@/types/api'

/** 两行固定布局，选择与代码预览独立；只读快照，不在向导里执行策略。 */
export function PortfolioChoiceCard({ portfolio, selected, onSelect, onPreview }: {
  portfolio: PortfolioLite
  selected: boolean
  onSelect: () => void
  onPreview: () => void
}) {
  const reader = useCallback((signal: AbortSignal) => getPortfolioTargetSnapshot(portfolio.id!, signal), [portfolio.id])
  const snapshot = usePolling(reader, { queryKey: `setup-portfolio:${portfolio.id}:${portfolio.updated_at}`, intervalMs: 0, enabled: portfolio.id != null })
  const state = portfolioTargetState(snapshot.data, snapshot.loading, snapshot.error, snapshot.stale)
  const calculated = state.kind === 'ready' || state.kind === 'empty'
  const summary = state.kind === 'ready' ? state.summary : null
  const holdings = state.kind === 'empty' ? '目标为空仓'
    : state.kind === 'loading' ? '加载目标持仓…'
    : state.kind === 'unavailable' ? '目标持仓读取失败'
    : '尚无目标持仓'

  return (
    <div className={`relative rounded-[14px] border transition-colors motion-reduce:transition-none ${selected ? 'border-accent bg-accent-soft shadow-[inset_0_0_0_1px_var(--color-accent)]' : 'border-line bg-surface hover:border-ink-3/30'}`}>
      <button
        type="button"
        aria-pressed={selected}
        aria-label={`选择组合：${portfolio.name}`}
        disabled={portfolio.id == null}
        onClick={onSelect}
        className="block w-full cursor-pointer rounded-[14px] px-[18px] py-3.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default disabled:opacity-45"
      >
        <span className="flex h-7 min-w-0 items-center gap-3 pr-[76px]">
          <OverflowText text={portfolio.name} className="min-w-0 max-w-[35%] text-[17px] font-[620]" />
          <span className="flex-none rounded-md bg-fill px-2 py-0.5 text-[12px] text-ink-2">{portfolio.market}</span>
          {summary && <span className="hidden flex-none items-center gap-3 text-[13px] text-ink-3 md:inline-flex">
            <span>{summary.activeCount} 个目标品种</span>
            <span>总敞口 <span className="tabular-nums text-ink-2">{formatTargetWeight(summary.grossExposure, false)}</span></span>
            <span>净敞口 <span className={`tabular-nums ${targetDirectionClass(summary.netExposure)}`}>{formatTargetWeight(summary.netExposure)}</span></span>
          </span>}
          {portfolio.description && <OverflowText text={portfolio.description} className="hidden min-w-0 flex-1 text-[13px] text-ink-3 lg:block" />}
        </span>
        <span className="mt-2 flex h-6 min-w-0 items-center gap-4">
          {summary && <span className="flex-none text-[12px] text-ink-3 md:hidden">{summary.activeCount} 个品种</span>}
          {summary ? <TargetChips entries={summary.entries} compact /> : <span className={`min-w-0 flex-1 text-[13px] ${state.kind === 'unavailable' ? 'text-warn' : 'text-ink-3'}`}><OverflowText text={holdings} /></span>}
          {calculated && <time dateTime={state.calculatedAt} title={`最近成功计算：${state.calculatedAt}`} className="hidden flex-none text-[12px] text-ink-3 sm:block">{formatTargetUpdatedAt(state.calculatedAt)}</time>}
          {snapshot.error && <span aria-hidden className="w-16 flex-none" />}
        </span>
      </button>
      <button type="button" aria-label={`预览代码：${portfolio.name}`} onClick={onPreview}
        className="absolute right-[18px] top-3.5 inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-md px-2 text-[13px] text-ink-2 hover:bg-fill hover:text-ink-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
        <Code2 size={16} aria-hidden />代码
      </button>
      <div className="px-[18px]"><ErrorNotice title={`${portfolio.name} 目标持仓读取失败`} error={snapshot.error} evidence={[{ label: '组合 ID', value: String(portfolio.id) }]} variant={snapshot.data ? 'stale' : 'compact'} updatedAt={snapshot.updatedAt} onRetry={snapshot.refresh} contextKey={portfolio.id ?? undefined} /></div>
    </div>
  )
}
