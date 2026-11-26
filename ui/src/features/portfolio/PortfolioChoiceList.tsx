import { useState } from 'react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { PortfolioCodePreview } from '@/features/portfolio/PortfolioCodePreview'
import { PortfolioChoiceCard } from '@/features/setup/PortfolioChoiceCard'
import { useDomainStore } from '@/stores/domain'
import type { PortfolioLite } from '@/types/api'

/** 开户与改绑共用组合列表；选择只更新调用方草稿，预览只读。 */
export function PortfolioChoiceList({ selectedId, onSelect, market, emptyMessage }: {
  selectedId: number | null
  onSelect: (id: number | null) => void
  market?: string
  emptyMessage?: string
}) {
  const allPortfolios = useDomainStore((s) => s.portfolios)
  const portfolios = market === undefined ? allPortfolios : allPortfolios?.filter((item) => item.market === market)
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
