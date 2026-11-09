import { useCallback } from 'react'
import { Outlet, useParams } from 'react-router'
import { ArchivedAccountDetail } from '@/features/account/ArchivedAccountDetail'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { getAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useDomainStore } from '@/stores/domain'

/** 旧编辑链接也遵守归档只读，不挂载连接和配置编辑器。 */
export function AccountEditGuard() {
  const { id } = useParams()
  const accountId = Number(id)
  const account = usePolling(useCallback((signal: AbortSignal) => getAccount(accountId, signal), [accountId]), {
    queryKey: `account-edit-guard:${accountId}`, intervalMs: 0,
  })
  const refresh = useDomainStore(s => s.refreshAccounts)
  if (account.data?.archived_at) return <ArchivedAccountDetail account={account.data} onChanged={async () => { await refresh(); await account.refresh() }} />
  if (!account.data) return account.error
    ? <ErrorNotice intent="read" title="账户加载失败" error={account.error} onRetry={account.refresh} />
    : <p className="text-sm text-ink-2">加载账户…</p>
  return <Outlet />
}
