import { useCallback } from 'react'
import { BackLink } from '@/components/ui/BackLink'
import { Card, Chip } from '@/components/ui/Card'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { ArchiveAction } from '@/features/account/ArchiveAction'
import { shanghaiLabel } from '@/features/history/costs'
import { getPortfolioTargetSnapshot } from '@/lib/api/portfolios'
import { usePolling } from '@/lib/hooks/usePolling'
import type { Portfolio } from '@/types/api'

/** 保存的源码和目标始终可读，归档详情没有试跑及保存入口。 */
export function ArchivedPortfolioDetail({ portfolio, onChanged }: {
  portfolio: Portfolio
  onChanged: () => void | Promise<void>
}) {
  const snapshot = usePolling(useCallback((signal: AbortSignal) => getPortfolioTargetSnapshot(portfolio.id!, signal), [portfolio.id]), {
    queryKey: `archived-portfolio:${portfolio.id}:snapshot`, intervalMs: 0,
  })
  return <section className="space-y-4 p-5">
    <BackLink to="/portfolios?archived=1" label="返回已归档组合" />
    <Card className="space-y-4 p-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">{portfolio.name}</h1><Chip>已归档</Chip>
        <ArchiveAction kind="portfolio" id={portfolio.id!} name={portfolio.name} archivedAt={portfolio.archived_at} onChanged={onChanged} />
      </div>
      <p className="text-sm text-ink-2">{portfolio.market} · 归档于 {shanghaiLabel(portfolio.archived_at!)}</p>
      {portfolio.description && <p className="text-sm">{portfolio.description}</p>}
      <h2 className="font-semibold">保存的组合代码</h2>
      <pre tabIndex={0} aria-label="保存的组合代码" className="max-h-[55vh] overflow-auto rounded-lg bg-fill p-4 text-sm">{portfolio.custom_calc_py_code}</pre>
      <h2 className="font-semibold">最后保存的目标快照</h2>
      <ErrorNotice intent="read" title="目标快照加载失败" error={snapshot.error} onRetry={snapshot.refresh} />
      {snapshot.loading && <p className="text-sm text-ink-2">加载目标快照…</p>}
      {snapshot.data && <>
        <p className="text-sm text-ink-2">{snapshot.data.calculated_at ? shanghaiLabel(snapshot.data.calculated_at) : '尚无目标快照'}</p>
        <pre tabIndex={0} className="overflow-auto rounded-lg bg-fill p-4 text-sm">{JSON.stringify(snapshot.data.raw_targets ?? snapshot.data.weights, null, 2)}</pre>
      </>}
    </Card>
  </section>
}
