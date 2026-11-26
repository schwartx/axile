import { OverflowText } from '@/components/ui/OverflowText'
import type { PortfolioMarketOption } from '@/features/portfolio/portfolioMarkets'

/** 组合和账户创建共用的市场选择卡。 */
export function MarketChoiceGroup({
  value,
  options,
  onChange,
  stacked = false,
}: {
  value: string
  options: PortfolioMarketOption[]
  onChange: (value: string) => void
  stacked?: boolean
}) {
  const orderedOptions = options.toSorted((a, b) => Number(b.label === 'A股') - Number(a.label === 'A股'))
  return (
    <div role="radiogroup" aria-label="市场" aria-orientation={stacked ? 'vertical' : undefined}
      className={`grid gap-px overflow-hidden rounded-[8px] border border-line bg-line ${stacked ? '' : 'sm:grid-cols-3'}`}>
      {orderedOptions.map((option) => {
        const selected = option.value === value
        const examples = option.exampleSymbols.slice(0, 2).join(' · ')
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={option.label}
            className={`relative flex min-h-[76px] min-w-0 items-center bg-surface px-4 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
              selected ? 'bg-accent-soft' : 'hover:bg-bg-subtle'
            }`}
            onClick={() => onChange(option.value)}
          >
            <span className="min-w-0 flex-1">
              <span className={`block text-[15px] font-[620] ${selected ? 'text-ink-1' : 'text-ink-2'}`}>{option.label}</span>
              {examples && <OverflowText className="mt-1 text-[13px] text-ink-3" text={examples} />}
            </span>
            {selected && <span aria-hidden className={`absolute inset-y-0 left-0 w-0.5 bg-accent ${stacked ? '' : 'sm:inset-x-0 sm:top-auto sm:bottom-0 sm:h-0.5 sm:w-auto'}`} />}
          </button>
        )
      })}
    </div>
  )
}
