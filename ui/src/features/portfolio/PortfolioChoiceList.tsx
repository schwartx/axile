import { Check } from 'lucide-react'
import { useState } from 'react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { PortfolioCodePreview } from '@/features/portfolio/PortfolioCodePreview'
import { PortfolioChoiceCard } from '@/features/setup/PortfolioChoiceCard'
import { useChannelCatalogStore } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import type { PortfolioLite } from '@/types/api'
import { portfolioMarketId } from './portfolioMarkets'

export function NoPortfolioButton({ selected, onSelect, disabled }: {
  selected: boolean
  onSelect: () => void
  disabled?: boolean
}) {
  return <button type="button" aria-pressed={selected} disabled={disabled} onClick={onSelect}
    className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-[13px] text-ink-2 hover:bg-fill focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-45">
    {selected && <Check size={14} aria-hidden />}不绑定组合
  </button>
}

/** 开户与改绑共用组合列表；选择只更新调用方草稿，预览只读。 */
export function PortfolioChoiceList({ selectedId, onSelect, market, emptyMessage }: {
  selectedId: number | null
  onSelect: (id: number | null) => void
  market?: string
  emptyMessage?: string
}) {
  const allPortfolios = useDomainStore((s) => s.portfolios)
  const channels = useChannelCatalogStore((s) => s.channels) ?? []
  const marketId = market === undefined ? undefined : portfolioMarketId(channels, market)
  const portfolios = marketId === undefined ? allPortfolios : allPortfolios?.filter(item => portfolioMarketId(channels, item.market) === marketId)
  const error = useDomainStore((s) => s.portfoliosError)
  const refresh = useDomainStore((s) => s.refreshPortfolios)
  const [preview, setPreview] = useState<PortfolioLite | null>(null)

  return (
    <div className="grid grid-cols-1 gap-3">
      <ErrorNotice intent="read" title="组合列表加载失败" error={error} onRetry={refresh} />
      {portfolios == null && !error && <p className="text-[15px] text-ink-2">加载组合…</p>}
      {portfolios?.length === 0 && <p className="text-[15px] text-ink-2">{emptyMessage ?? (market === undefined ? '还没有组合。' : '该市场还没有组合。')}</p>}
      <fieldset disabled={Boolean(error)} className="grid min-w-0 grid-cols-1 gap-3">
        {portfolios?.map((p) => (
          <PortfolioChoiceCard key={p.id} portfolio={p} selected={selectedId === p.id && p.id != null}
            onSelect={() => onSelect(p.id)} onPreview={() => setPreview(p)} />
        ))}
        {preview && <PortfolioCodePreview portfolio={preview} selected={selectedId === preview.id && preview.id != null}
          onClose={() => setPreview(null)} onSelect={() => { onSelect(preview.id); setPreview(null) }} />}
      </fieldset>
    </div>
  )
}
