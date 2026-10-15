import { useMemo } from 'react'
import { Plus } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { useDomainStore } from '@/stores/domain'
import { FleetView, FleetSkeleton } from '@/features/dashboard/FleetView'
import { ErrorNotice } from '@/components/ui/ErrorNotice'

/** 所有账户列表：不论几个账户都是稳定的舰队着陆页，点卡片进入单账户详情。 */
export function DashboardPage() {
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
