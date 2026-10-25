import { useAccountConnection } from '@/stores/liveConnection'
import { useConnectedAssets } from '@/features/account/useConnectedAssets'
import { useServiceDisconnected } from '@/lib/errorPresentation'
import { useCallback, useEffect, useRef } from 'react'
import { useParams } from 'react-router'
import { AccountConnectionStatus } from '@/features/account/AccountConnectionStatus'
import { AccountConnectionContext } from '@/lib/accountConnection'
import { Card } from '@/components/ui/Card'
import { Skeleton, SkeletonLines } from '@/components/ui/Skeleton'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { HoldingsView } from '@/features/account/HoldingsView'
import { AccountPageTitle } from '@/features/account/pageHead'
import { accountAssetTerms } from '@/features/dashboard/display'
import { TargetSnapshotControl } from '@/features/portfolio/TargetSnapshotControl'
import { getAccount, getAccountAssetSnapshots, getAccountRebalancePlan, getAccountTargetSnapshot, refreshAccountTargetSnapshot } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useTargetSnapshot } from '@/lib/hooks/useTargetSnapshot'
import { currencyOf, observedTotalAsset, positionsOfAssets } from '@/lib/derive'
import { useDomainStore } from '@/stores/domain'
import { useChannelDescriptor } from '@/stores/channels'
import { isExecutingStatus } from '@/features/dashboard/execProgress'
import { executionJustSettled, useRunning } from '@/stores/liveExec'

/**
 * 持仓明细路由页 /accounts/:id/holdings。
 *
 * 由抽屉升级而来：换回可深链/刷新/后退的 URL。账户名读自共享 store，
 * 权益与持仓共用最近一次资产快照（缺快照才退回仪表盘权益），目标取自组合最新权重。
 */
export function AccountHoldingsPage() {
  const { id } = useParams()
  const accountId = Number(id)
  const connection = useAccountConnection(accountId)
  const disconnected = useServiceDisconnected()
  const accounts = useDomainStore((s) => s.accounts)
  const refreshAccounts = useDomainStore((s) => s.refreshAccounts)
  const item = accounts?.find((a) => a.account_id === accountId) ?? null
  const accountEvidence = [{ label: '账户', value: `${item?.name ?? ''} (#${accountId})` }]
  const assetTerms = accountAssetTerms(item?.trade_channel)
  const descriptor = useChannelDescriptor(item?.trade_channel)
  const running = useRunning(accountId)

  const account = usePolling(useCallback((s: AbortSignal) => getAccount(accountId, s), [accountId]), {
    queryKey: `account:${accountId}`,
    intervalMs: 15000,
  })
  const snapshots = usePolling(
    useCallback((s: AbortSignal) => getAccountAssetSnapshots(accountId, { limit: 1 }, s), [accountId]),
    { queryKey: `account:${accountId}:asset-snapshots:1`, intervalMs: 10000 },
  )
  // 目标改取账户级「执行器口径」权重（后端已叠加杠杆与精度），与含杠杆的真实持仓同尺。
  const weights = useTargetSnapshot(
    useCallback((s: AbortSignal) => getAccountTargetSnapshot(accountId, s), [accountId]),
    useCallback(() => refreshAccountTargetSnapshot(accountId), [accountId]),
    `account:${accountId}:target-snapshot`,
  )
  const comparison = usePolling(
    useCallback((s: AbortSignal) => getAccountRebalancePlan(accountId, s), [accountId]),
    { queryKey: `account:${accountId}:rebalance-plan`, intervalMs: 10000 },
  )

  const latestAssets = snapshots.data?.data[0]?.assets
  const positions = positionsOfAssets(latestAssets)
  const target = weights.data?.weights ?? {}
  const equity = observedTotalAsset(latestAssets, item?.total_asset)
  const previousLiveRef = useRef(running)
  const refreshSnapshots = snapshots.refresh
  const reloadTargetSnapshot = weights.reloadSnapshot
  const refreshComparison = comparison.refresh
  const connectedAssets = useConnectedAssets(accountId, () => {
    void refreshSnapshots()
    void refreshComparison()
    void account.refresh()
  })
  useEffect(() => {
    if (executionJustSettled(previousLiveRef.current, running)) {
      void refreshSnapshots()
      void reloadTargetSnapshot()
      void refreshComparison()
      void refreshAccounts()
    }
    previousLiveRef.current = running
  }, [running, refreshSnapshots, reloadTargetSnapshot, refreshComparison, refreshAccounts])
  const tradeChannel = item?.trade_channel ?? account.data?.trade_channel
  const portfolioId = item?.portfolio_id ?? account.data?.portfolio_id ?? null
  // 快照缺失但实时口径（dashboard.holdings_count）显示有持仓：资产观测未返回有效持仓明细，
  // 逐只对照会把「实际持有」误判为空仓并给出「买入建仓」的危险建议。此时降级为「待刷新」，
  // 不展示可能翻倍的调仓计划（对齐 derive.ts 头部「不能确定的不编」原则）。
  const holdingsCount = item?.holdings_count ?? 0
  const holdingsStale = !Array.isArray(latestAssets?.positions) || (positions.length === 0 && holdingsCount > 0)

  return (
    <AccountConnectionContext.Provider value={{ accountId, handled: !disconnected && !account.error && connection.status === 'unavailable' }}><section>
      <div className="flex flex-wrap items-center gap-3">
        <AccountPageTitle
          accountId={accountId}
          page="持仓明细"
          name={item?.name ?? account.data?.name}
          channel={tradeChannel}
          market={item?.market ?? account.data?.market}
        />
        <AccountConnectionStatus accountId={accountId} />
      </div>

      <Card className="mt-4 px-6 py-5">
        <div className="mb-3 border-b border-line pb-3">
          <TargetSnapshotControl
            evidence={accountEvidence}
            contextKey={accountId}
            snapshot={weights.data}
            loading={weights.loading}
            recalculating={weights.recalculating}
            error={weights.recalculateError}
            readError={weights.error}
            disabled={connection.status !== 'ready' || isExecutingStatus(running?.status) || portfolioId == null}
            disabledReason={connection.status !== 'ready' ? '连接账户后可重新计算' : isExecutingStatus(running?.status) ? '账户正在执行，结束后可重新计算' : portfolioId == null ? '账户未绑定组合' : undefined}
            onRecalculate={() => void weights.recalculate().then(() => account.refresh())}
          />
        </div>
        {(!snapshots.data || !weights.data || !comparison.data) && (snapshots.loading || weights.loading || comparison.loading) ? (
          <div aria-label="正在加载持仓与目标" aria-busy="true">
            <div className="flex items-center justify-between border-b border-line pb-3">
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-4 w-20" />
            </div>
            <SkeletonLines rows={6} className="mt-3" />
          </div>
        ) : (!snapshots.data && snapshots.error) || (!weights.data && weights.error) || (!comparison.data && comparison.error) ? null : !weights.data?.calculated_at ? (
          <div className="text-[15px] text-ink-3">尚无目标权重，点击刷新按钮计算后再查看持仓对照。</div>
        ) : holdingsStale ? (
          <div className="text-[15px] leading-relaxed text-warn">
            {!latestAssets ? '尚未查询账户资产。' : !Array.isArray(latestAssets.positions)
              ? '持仓数据待刷新：资产观测未返回有效持仓明细。'
              : `持仓数据待刷新：当前持有 ${holdingsCount} 个品种，但资产观测未返回持仓明细。`}
            <span className="mt-1 block text-ink-3">
              为避免给出「买入建仓」等错误调仓建议，这里暂不展示逐只对照；在账户详情刷新账户资产后即可查看。
            </span>
          </div>
        ) : (
          <HoldingsView
            positions={positions}
            executablePlan={comparison.data}
            target={target}
            equity={equity}
            currency={currencyOf(item?.currency)}
            assetLabel={assetTerms.shortLabel}
            sizing={weights.data?.sizing ?? null}
            quantityLabel={descriptor?.units.quantity_label ?? ''}
          />
        )}
        <ErrorNotice accountId={accountId} intent="read" evidence={accountEvidence} title="账户详情读取失败" error={account.error} hasData={account.data != null} onRetry={account.refresh} contextKey={accountId} />
        <ErrorNotice accountId={accountId} intent="read" evidence={accountEvidence} title="连接后的资产更新未完成" error={connectedAssets.error} contextKey={accountId} />
        <ErrorNotice accountId={accountId} intent="read" evidence={accountEvidence} title="资产快照读取失败" error={snapshots.error} hasData={snapshots.data != null} variant={snapshots.data ? 'stale' : 'section'}
          updatedAt={snapshots.updatedAt} onRetry={snapshots.refresh} contextKey={accountId} />
        <ErrorNotice accountId={accountId} intent="read" evidence={accountEvidence} title="目标快照读取失败" error={weights.error} hasData={weights.data != null} variant={weights.data ? 'stale' : 'section'}
          updatedAt={weights.updatedAt} onRetry={weights.reloadSnapshot} contextKey={accountId} />
        <ErrorNotice accountId={accountId} intent="read" evidence={accountEvidence} title="调仓计划读取失败" error={comparison.error} hasData={comparison.data != null} variant={comparison.data ? 'stale' : 'section'}
          updatedAt={comparison.updatedAt} onRetry={comparison.refresh} contextKey={accountId} />
      </Card>
    </section></AccountConnectionContext.Provider>
  )
}
