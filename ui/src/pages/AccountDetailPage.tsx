import { useCallback } from 'react'
import { getAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { ArchivedAccountDetail } from '@/features/account/ArchivedAccountDetail'
import { useParams } from 'react-router'
import { useDomainStore } from '@/stores/domain'
import { AccountDetail, AccountDetailSkeleton } from '@/features/account/AccountDetail'
import { ErrorNotice } from '@/components/ui/ErrorNotice'

/** 账户详情路由 /accounts/:id —— 从共享 store 读该账户，复用原子视图。 */
export function AccountDetailPage() {
  const { id } = useParams()
  const accountId = Number(id)
  const accounts = useDomainStore((s) => s.accounts)
  const error = useDomainStore((s) => s.accountsError)
  const refresh = useDomainStore((s) => s.refreshAccounts)

  const item = accounts?.find((a) => a.account_id === accountId)

  const saved = usePolling(useCallback((signal: AbortSignal) => getAccount(accountId, signal), [accountId]), {
    queryKey: `account-detail:${accountId}`, intervalMs: 0, enabled: accounts != null && !item,
  })
  if (saved.data?.archived_at) return <ArchivedAccountDetail account={saved.data} onChanged={async () => { await refresh(); await saved.refresh() }} />

  return (
    <section>
      {accounts == null && !error && <AccountDetailSkeleton />}
      <ErrorNotice intent="read" title="账户数据加载失败" error={accounts == null ? error : null} onRetry={refresh} />
      {!item && <ErrorNotice intent="read" title="账户详情加载失败" error={saved.error} onRetry={saved.refresh} />}
      {accounts != null && !error && !item && !saved.loading && !saved.data && !saved.error && <p className="text-[15px] text-ink-3">未找到账户 #{accountId}。</p>}
      {item && (
        <AccountDetail key={accountId}
          accountId={accountId}
          item={item}
          onDashboardRefresh={refresh}
        />
      )}
    </section>
  )
}
