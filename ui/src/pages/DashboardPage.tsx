import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router'
import { Segmented } from '@/components/ui/Segmented'
import { Card } from '@/components/ui/Card'
import { ArchiveAction } from '@/features/account/ArchiveAction'
import { getDashboard } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { withViewTransition } from '@/lib/viewTransition'
import { shanghaiLabel } from '@/features/history/costs'
import { Plus } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { useDomainStore } from '@/stores/domain'
import { FleetView, FleetSkeleton } from '@/features/dashboard/FleetView'
import { ErrorNotice } from '@/components/ui/ErrorNotice'

/** 所有账户列表：不论几个账户都是稳定的舰队着陆页，点卡片进入单账户详情。 */
function CurrentAccounts() {
  const accounts = useDomainStore((s) => s.accounts)
  const portfolios = useDomainStore((s) => s.portfolios)
  const error = useDomainStore((s) => s.accountsError)
  const refresh = useDomainStore((s) => s.refreshAccounts)

  const nameMap = useMemo(() => {
    const m = new Map<number, string>()
    for (const p of portfolios ?? []) if (p.id != null) m.set(p.id, p.name)
    return m
  }, [portfolios])

  // 首次加载：无数据时才 loading / 报错；已有数据则一直渲染（失联降级由顶栏体现）。
  if (accounts == null) {
    if (error) return <ErrorNotice intent="read" title="账户加载失败" error={error} onRetry={refresh} />
    return <FleetSkeleton />
  }

  if (accounts.length === 0) {
    return (
      <div className="rounded-card border border-line bg-surface px-8 py-14 text-center">
        <p className="text-[17px] font-[650]">还没有账户</p>
        <p className="mt-2 text-[14.5px] leading-relaxed text-ink-2">
          连接交易所账户后，axile 会接管它的持仓目标与定时执行。
        </p>
        <Link
          to="/setup/acct/channel"
          className="mt-6 inline-flex items-center gap-1.5 rounded-chip bg-accent px-4 py-2 text-[14px] font-medium text-white transition-colors duration-150 hover:brightness-110 motion-reduce:transition-none"
        >
          <Plus size={14} aria-hidden />新建账户
        </Link>
      </div>
    )
  }

  return <FleetView items={accounts} portfolioNames={nameMap} />
}


/** 归档列表按需读取，不进入日常舰队的汇总和轮询。 */
export function DashboardPage() {
  const [params, setParams] = useSearchParams()
  const archived = params.get('archived') === '1'
  const refresh = useDomainStore(s => s.refreshAccounts)
  const archive = usePolling(useCallback((signal: AbortSignal) => getDashboard(signal, true), []), {
    queryKey: 'archived-accounts', intervalMs: 0, enabled: archived,
  })
  return <section>
    <div className="mb-4"><Segmented value={archived ? 'archived' : 'current'} size="sm" ariaLabel="账户范围"
      options={[{ value: 'current', label: '当前' }, { value: 'archived', label: '已归档' }]}
      onChange={value => withViewTransition(() => { const next = new URLSearchParams(params); if (value === 'archived') next.set('archived', '1'); else next.delete('archived'); setParams(next) })} /></div>
    {!archived ? <CurrentAccounts /> : <div className="space-y-3">
      <ErrorNotice intent="read" title="已归档账户加载失败" error={archive.error} onRetry={archive.refresh} />
      {archive.loading && <p className="text-sm text-ink-2">加载已归档账户…</p>}
      {archive.data?.data.length === 0 && <p className="text-sm text-ink-2">没有已归档账户。</p>}
      {archive.data?.data.map(account => <Card key={account.account_id} className="flex flex-wrap items-center justify-between gap-3 p-5">
        <div><Link to={`/accounts/${account.account_id}`} className="font-semibold hover:text-accent">{account.name}</Link>
          <p className="mt-1 text-sm text-ink-2">归档于 {shanghaiLabel(account.archived_at!)}</p></div>
        <ArchiveAction kind="account" id={account.account_id} name={account.name} archivedAt={account.archived_at}
          onChanged={async () => { await refresh(); await archive.refresh() }} />
      </Card>)}
    </div>}
  </section>
}
