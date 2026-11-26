import { useCallback, useState } from 'react'
import { useViewTransitionState } from 'react-router'
import { Card, Chip } from '@/components/ui/Card'
import { NotificationIndicator } from '@/features/account/NotificationIndicator'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Link, useNavigate } from '@/components/ui/nav'
import { AccountOverviewConfigButton, AccountOverviewEquity, AccountOverviewIdentity, AccountOverviewPortfolio } from '@/features/account/AccountOverviewHero'
import { AssetRefreshFeedback } from '@/features/account/AssetRefreshFeedback'
import { describeLeverage } from '@/features/account/configSummary'
import { HoldingsPreviewTable } from '@/features/account/HoldingsPreviewTable'
import { signalHoldingPreviewRows } from '@/features/account/holdingDetailModel'
import { formatBeijingTimestamp, formatObservedAt, formatPlannedAt, formatRecentExecution } from '@/lib/scheduleTime'
import { portfolioMarketDescriptor } from '@/features/portfolio/portfolioMarkets'
import { describeCron } from '@/features/setup/cron'
import { TimerQuickModal } from '@/features/setup/TimerQuickModal'
import { getSignalConfig, getSignalRecords } from '@/features/signal/signalApi'
import { SignalRecordList } from '@/features/signal/SignalRecordList'
import { deleteAccount, getAccount, getAccountAssetSnapshots, getAccountTargetSnapshot, getNextRun, refreshAccountAssets, refreshAccountTargetSnapshot, runAccountSignal, updateAccount } from '@/lib/api/accounts'
import { currentEquity } from '@/features/dashboard/performance'
import { usePolling } from '@/lib/hooks/usePolling'
import { shanghaiLabel } from '@/features/history/costs'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import { AccountActions } from '@/features/account/AccountActions'
import { ArchiveAction } from '@/features/account/ArchiveAction'
import { CopyAccountDialog } from '@/features/account/CopyAccountDialog'
import { AccountConfigDialog } from '@/features/account/AccountConfigDialog'
import { TEXT } from '@/features/account/editUi'
import { AccountLeverageDialog } from '@/features/account/AccountLeverageDialog'
import type { AccountDashboardItem } from '@/types/api'

export function SignalAccountDetail({ item }: { item: AccountDashboardItem }) {
  const id = item.account_id
  const navigate = useNavigate()
  const toast = useToastStore((state) => state.toast)
  const settingsTo = `/accounts/${id}/edit`
  const enteringOverview = useViewTransitionState(`/accounts/${id}`)
  const account = usePolling(useCallback((signal: AbortSignal) => getAccount(id, signal), [id]),
    { queryKey: `signal-account:${id}`, intervalMs: 15000 })
  const config = usePolling(useCallback((signal: AbortSignal) => getSignalConfig(id, signal), [id]),
    { queryKey: `signal-config:${id}`, intervalMs: 0 })
  const targets = usePolling(useCallback((signal: AbortSignal) => getAccountTargetSnapshot(id, signal), [id]),
    { queryKey: `signal-targets:${id}:${item.portfolio_id}`, intervalMs: 15000, enabled: item.portfolio_id != null })
  const assets = usePolling(useCallback((signal: AbortSignal) => getAccountAssetSnapshots(id, { limit: 1 }, signal), [id]),
    { queryKey: `signal-assets:${id}`, intervalMs: 15000 })
  const records = usePolling(useCallback((signal: AbortSignal) => getSignalRecords(id, { limit: 6 }, signal), [id]),
    { queryKey: `recent-signals:${id}`, intervalMs: 15000 })
  const nextRun = usePolling(useCallback((signal: AbortSignal) => getNextRun(id, signal), [id]),
    { queryKey: `account:${id}:next-run`, intervalMs: 30000, enabled: item.is_scheduled })
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const [editing, setEditing] = useState<'basic' | 'leverage' | 'copy' | 'timer' | null>(null)
  const closeSettings = () => setEditing(null)
  const refreshSettings = () => {
    closeSettings()
    void account.refresh()
    void refreshAccounts()
  }
  const signalChannel = useChannelDescriptor(item.trade_channel)
  const portfolioName = useDomainStore((state) => state.portfolios?.find((p) => p.id === item.portfolio_id)?.name)
  const channels = useChannelCatalogStore((state) => state.channels)
  const isStarted = account.data?.is_started ?? item.is_started
  type Action = 'assets' | 'target' | 'signal' | 'toggle' | 'delete'
  const [busy, setBusy] = useState<Action | null>(null)
  const [error, setError] = useState<{ action: Action; cause: Error } | null>(null)
  const act = async (action: Action) => {
    if (busy !== null) return
    setBusy(action)
    setError(null)
    try {
      if (action === 'assets') {
        await refreshAccountAssets(id)
        await Promise.all([assets.refresh(), refreshAccounts()])
      } else if (action === 'target') {
        await refreshAccountTargetSnapshot(id)
        await targets.refresh()
      } else if (action === 'signal') {
        await runAccountSignal(id)
        await Promise.all([targets.refresh(), assets.refresh(), records.refresh(), account.refresh(), refreshAccounts()])
        toast('执行完成')
      } else if (action === 'toggle') {
        const updated = await updateAccount(id, { is_started: !isStarted })
        await Promise.all([account.refresh(), nextRun.refresh(), refreshAccounts()])
        toast(updated.runtime_sync?.status === 'synchronized'
          ? updated.is_started ? '已启动' : '已暂停'
          : '设置已保存，运行态待同步')
      } else {
        await deleteAccount(id)
        await refreshAccounts()
        toast('账户已删除')
        navigate('/')
      }
    } catch (cause) {
      setError({ action, cause: cause instanceof Error ? cause : new Error(String(cause)) })
    } finally {
      setBusy(null)
    }
  }

  const latest = assets.data?.data[0]
  const latestExtra = latest?.assets.extra
  const positionsKnown = latestExtra != null && typeof latestExtra === 'object'
    && 'positions_known' in latestExtra && latestExtra.positions_known === true
  const positions = positionsKnown ? latest?.assets.positions ?? [] : undefined
  const equity = latest?.assets.total_asset ?? currentEquity(item)
  const currency = latest?.assets.currency ?? item.currency
  const availableCash = latest?.assets.available_cash
  const snapshot = targets.data?.calculated_at ? targets.data : null
  const holdings = signalHoldingPreviewRows(positions, equity ?? 0, snapshot)
  const holdingsCount = new Set(holdings.filter((row) => !row.targetOnly).map((row) => row.symbol)).size
  const quantities = snapshot?.target_mode === 'quantity' ? snapshot.raw_targets ?? {} : snapshot?.quantities
  const targetCount = quantities ? Object.values(quantities).filter((quantity) => quantity !== 0).length : null
  const market = portfolioMarketDescriptor(channels ?? [], item.market)
  const schedule = market?.schedule
  const cron = account.data?.cron_expr
  const rhythm = cron && schedule ? describeCron(schedule.kind, cron, schedule.night, account.data?.channel_options.gm) : null
  const next = item.is_scheduled ? nextRun.data?.next_execution_times[0] : null
  const notification = account.data?.notification_state
  const lastPushed = notification?.last_success_at
  const latestSignal = records.data?.items[0]
  const longLeverage = account.data?.long_leverage ?? item.long_leverage
  const shortLeverage = account.data?.short_leverage ?? item.short_leverage

  return <>
    <Card className="p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <AccountOverviewIdentity item={item} nameTransition={enteringOverview} />
        {!isStarted && <Chip>已暂停</Chip>}
        <AccountActions signal name={item.name} isStarted={isStarted} running={busy === 'signal'}
          runDisabledReason={item.portfolio_id == null ? '绑定组合后可执行'
            : busy && busy !== 'signal' ? '当前操作结束后可执行' : undefined}
          toggling={busy !== null}
          archiveAction={(className) => <ArchiveAction kind="account" id={id} name={item.name} className={className}
            onChanged={async () => { await refreshAccounts(); await account.refresh() }} />}
          onExec={() => void act('signal')} onToggleStarted={() => void act('toggle')}
          onEdit={() => setEditing('basic')} onCopy={() => setEditing('copy')} onDelete={() => void act('delete')} />
      </div>
      {item.remark && <p className="my-2 break-words text-[14px] text-ink-2">{item.remark}</p>}
      <AccountOverviewPortfolio portfolioId={item.portfolio_id} portfolioName={portfolioName ?? null}
        editTo={`${settingsTo}/portfolio`} showBindAction />
      <div className="mt-2">
        <AccountOverviewEquity accountId={id} equity={equity} currency={currency}
          observedAt={latest?.created_at ?? item.asset_observed_at}
          refreshing={busy === 'assets'}
          refreshDisabledReason={busy && busy !== 'assets' ? '当前操作结束后可刷新资产' : undefined}
          onRefresh={() => void act('assets')}
          feedback={<AssetRefreshFeedback accountId={id} manualError={error?.action === 'assets' ? error.cause : null} automaticError={null} />}
          summary={typeof availableCash === 'number' && <span className="num text-[14.5px] text-ink-2">
            可用资金 {availableCash.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}
          </span>}
        />
      </div>
      <div className="mt-1.5 flex min-h-6 min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-ink-3">
        {latestSignal ? <>
          <time dateTime={latestSignal.created_at}>上次 {formatRecentExecution(latestSignal.created_at)}</time>
          <Link to={`/accounts/${id}/signals`}
            className={`text-[14px] font-semibold tracking-tight hover:underline ${latestSignal.status === 'FAILED' ? 'text-warn' : 'text-ink-1'}`}>
            {latestSignal.status === 'FAILED' ? '执行失败' : '执行完成'} →
          </Link>
        </> : records.data && '尚无执行'}
      </div>
      {(longLeverage != null || shortLeverage != null || account.data) && (
        <div className="mt-2 flex items-center gap-2 border-t border-line pt-2 text-[14px]">
          {(longLeverage != null || shortLeverage != null) && (
            <AccountOverviewConfigButton label="杠杆" title="编辑杠杆"
              value={describeLeverage(longLeverage ?? null, shortLeverage ?? null, market?.ui.show_short_leverage ?? false)}
              disabled={!account.data || !signalChannel || !market} onClick={() => setEditing('leverage')} />
          )}
          {account.data && <div className="ml-auto flex items-center">
            <NotificationIndicator account={account.data} />
          </div>}
        </div>
      )}
      <ErrorNotice title="账户操作失败" intent="mutation" error={error?.action !== 'assets' ? error?.cause : null} />
    </Card>

    <CopyAccountDialog accountId={id} name={item.name} isStarted={isStarted} isBusy={busy !== null}
      open={editing === 'copy'} onClose={closeSettings} />
    {editing === 'basic' && <SignalAccountBasicDialog item={item} onClose={closeSettings} onSaved={refreshSettings} />}
    {editing === 'leverage' && account.data && signalChannel && market && <AccountLeverageDialog
      accountId={id} account={account.data} channel={{ ...signalChannel, ui: market.ui }}
      onClose={closeSettings} onSaved={refreshSettings}
    />}

    <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
      <Card className="min-w-0 px-6 py-4 md:col-span-2">
        <div className="mb-3 flex min-h-8 min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          <h3 className="text-[14px] font-semibold text-ink-2">持仓 vs 目标</h3>
          {snapshot?.calculated_at && <time dateTime={snapshot.calculated_at}
            title={formatBeijingTimestamp(snapshot.calculated_at)} className="text-[13px] text-ink-3">
            目标计算于 {formatObservedAt(snapshot.calculated_at)}
          </time>}
          {item.portfolio_id != null && <button type="button" disabled={busy !== null}
            className="ml-auto cursor-pointer text-[13.5px] font-semibold text-accent enabled:hover:underline disabled:cursor-default disabled:opacity-50"
            onClick={() => void act('target')}>
            {busy === 'target' ? '计算中…' : '计算目标'}
          </button>}
        </div>
        <div className={`min-w-0 md:min-h-48 ${holdings.length ? 'grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.618fr)]' : ''}`}>
          <div className="space-y-1 border-t border-line pt-2.5 text-[14px] text-ink-2">
            <p>{item.portfolio_id == null ? '绑定组合后可计算目标'
              : targetCount != null ? `目标持仓 ${targetCount} 个品种`
                : targets.loading ? '正在读取目标' : targets.error ? '目标暂不可用' : snapshot ? '数量待换算' : '尚未计算目标'}</p>
            <p className="text-ink-3">{positions === undefined
              ? latest ? '未提供实际持仓' : assets.loading ? '正在读取持仓' : assets.error ? '实际持仓暂不可用' : '尚未查询实际持仓'
              : holdingsCount ? `实际持仓 ${holdingsCount} 个品种` : '当前空仓'}</p>
          </div>
          {holdings.length > 0 && <div className="min-w-0 border-t border-line lg:border-t-0 lg:border-l lg:pl-5">
            <HoldingsPreviewTable observed={positionsKnown} holdings={holdings} quantityMode
              currency={currency} quantityLabel={config.data?.quantity_unit}
              positionValueLabel={holdings.some((row) => row.value != null) ? '市值' : undefined} compact />
          </div>}
        </div>
        <ErrorNotice intent="read" title="目标快照读取失败" error={targets.error} hasData={snapshot != null} onRetry={targets.refresh} />
        <ErrorNotice intent="read" title="资产快照读取失败" error={assets.error} hasData={latest != null} onRetry={assets.refresh} />
        <ErrorNotice intent="read" title="信号配置读取失败" error={config.error} onRetry={config.refresh} />
      </Card>

      <Card className="min-w-0 px-6 py-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h3 className="text-[14px] font-semibold text-ink-2">自动执行</h3>
          <button type="button" onClick={() => setEditing('timer')} disabled={!account.data || !market}
            className="cursor-pointer text-[13.5px] font-semibold text-accent enabled:hover:underline disabled:cursor-default disabled:text-ink-3">
            调整
          </button>
        </div>
        <dl className="divide-y divide-line border-t border-line text-[15px]">
          <div className="flex justify-between gap-3 py-1.5">
            <dt className="shrink-0 text-ink-2">节奏</dt>
            <dd className="min-w-0 text-right font-medium">{cron ? rhythm ?? '自定义节奏' : account.loading ? '读取中…' : account.error ? '—' : '未设置'}</dd>
          </div>
          <div className="flex justify-between gap-3 py-1.5">
            <dt className="shrink-0 text-ink-2">下次计算</dt>
            <dd className="text-right font-medium">{next
              ? <time dateTime={next} title={formatBeijingTimestamp(next)}>{formatPlannedAt(next)}</time>
              : nextRun.loading && item.is_scheduled ? '读取中…'
                : nextRun.error && item.is_scheduled ? '—' : cron && !item.is_started ? '已暂停' : '尚未排程'}</dd>
          </div>
          <div className="flex justify-between gap-3 py-1.5">
            <dt className="shrink-0 text-ink-2">最近推送</dt>
            <dd className="min-w-0 text-right font-medium">
              {notification?.last_attempt_ok === false
                ? <span className="break-words text-warn">{notification.last_attempt_error || '上次推送失败'}</span>
                : lastPushed ? <time dateTime={lastPushed} title={formatBeijingTimestamp(lastPushed)}>{shanghaiLabel(lastPushed)}</time>
                  : account.loading ? '读取中…' : account.error ? '—' : '暂无推送'}
            </dd>
          </div>
        </dl>
        <ErrorNotice title="账户读取失败" intent="read" error={account.error} hasData={account.data != null} onRetry={account.refresh} />
        <ErrorNotice title="定时计划读取失败" intent="read" error={item.is_scheduled ? nextRun.error : null} hasData={nextRun.data != null} onRetry={nextRun.refresh} />
      </Card>
    </div>
    <section className="mt-6">
      <div className="mx-0.5 mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xs font-semibold tracking-wide text-ink-3">近期信号</h2>
        <Link to={`/accounts/${id}/signals`} className="text-[13.5px] font-semibold text-accent hover:underline">
          完整信号记录 →
        </Link>
      </div>
      <Card className="overflow-hidden"><SignalRecordList poll={records} compact /></Card>
    </section>
    {market && account.data && <TimerQuickModal open={editing === 'timer'} accountId={id} accountName={item.name}
      tradeChannel={item.trade_channel} scheduleChannel={market.channel} isStarted={isStarted}
      cronExpr={cron ?? ''} channelOptions={account.data.channel_options} onClose={closeSettings}
      onSaved={() => { void account.refresh(); void nextRun.refresh(); void refreshAccounts() }} />}
  </>
}

function SignalAccountBasicDialog({ item, onClose, onSaved }: {
  item: AccountDashboardItem
  onClose: () => void
  onSaved: () => void
}) {
  const [baseline] = useState(() => ({ name: item.name, remark: item.remark ?? '' }))
  const [name, setName] = useState(baseline.name)
  const [remark, setRemark] = useState(baseline.remark)
  const accountName = name.trim()
  const patch = accountName ? {
    ...(accountName !== baseline.name ? { name: accountName } : {}),
    ...(remark !== baseline.remark ? { remark: remark || null } : {}),
  } : null

  return (
    <AccountConfigDialog accountId={item.account_id} accountName={baseline.name} title="基本信息"
      patch={patch} onClose={onClose} onSaved={onSaved}>
      <label className="grid gap-1 text-sm text-ink-2">
        名称
        <input className={TEXT} value={name} required aria-invalid={!accountName}
          aria-describedby={accountName ? undefined : 'signal-account-name-error'} onChange={(event) => setName(event.target.value)} />
      </label>
      {!accountName && <p id="signal-account-name-error" className="text-xs text-warn">名称不能为空</p>}
      <label className="grid gap-1 text-sm text-ink-2">
        备注
        <input className={TEXT} value={remark} placeholder="可选" onChange={(event) => setRemark(event.target.value)} />
      </label>
    </AccountConfigDialog>
  )
}
