import { Check } from 'lucide-react'
import { useCallback, useState } from 'react'
import { useParams } from 'react-router'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Link } from '@/components/ui/nav'
import { CashFlowPanel } from '@/features/account/CashFlowPanel'
import { EditLoading, EditSaveBar, Section, Toggle } from '@/features/account/editUi'
import { AccountPageTitle } from '@/features/account/pageHead'
import { PortfolioChoiceList } from '@/features/portfolio/PortfolioChoiceList'
import { WeightResult } from '@/features/portfolio/WeightResult'
import { SignalSources, signalAction } from '@/features/signal/SignalSources'
import { getSignalConfig } from '@/features/signal/signalApi'
import { configToSignalDraft, initialSignalDraft, signalConfig } from '@/features/signal/signalData'
import { getAccount, getAccountAssetSnapshots, getAccountTargetSnapshot, refreshAccountAssets, refreshAccountTargetSnapshot, updateAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { shanghaiLabel } from '@/features/history/costs'
import { useDomainStore } from '@/stores/domain'
import type { Account, AccountDashboardItem, Position } from '@/types/api'

export function SignalAccountDetail({ item }: { item: AccountDashboardItem }) {
  const id = item.account_id
  const account = usePolling(useCallback((signal: AbortSignal) => getAccount(id, signal), [id]),
    { queryKey: `signal-account:${id}`, intervalMs: 15000 })
  const config = usePolling(useCallback((signal: AbortSignal) => getSignalConfig(id, signal), [id]),
    { queryKey: `signal-config:${id}`, intervalMs: 0 })
  const targets = usePolling(useCallback((signal: AbortSignal) => getAccountTargetSnapshot(id, signal), [id]),
    { queryKey: `signal-targets:${id}`, intervalMs: 15000 })
  const assets = usePolling(useCallback((signal: AbortSignal) => getAccountAssetSnapshots(id, { limit: 1 }, signal), [id]),
    { queryKey: `signal-assets:${id}`, intervalMs: 15000 })
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const portfolioName = useDomainStore((state) => state.portfolios?.find((p) => p.id === item.portfolio_id)?.name)
  const [busy, setBusy] = useState<'assets' | 'target' | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const act = async (name: 'assets' | 'target') => {
    setBusy(name)
    setError(null)
    try {
      if (name === 'assets') {
        await refreshAccountAssets(id)
        await Promise.all([assets.refresh(), refreshAccounts()])
      } else {
        await refreshAccountTargetSnapshot(id)
        await targets.refresh()
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      setBusy(null)
    }
  }
  const latest = assets.data?.data[0]
  const latestExtra = latest?.assets.extra
  const known = latestExtra != null && typeof latestExtra === 'object' && 'positions_known' in latestExtra && latestExtra.positions_known === true
  const positions: Position[] | undefined = known ? latest?.assets.positions ?? [] : undefined
  const notification = account.data?.notification_state
  return <div className="mx-auto grid min-w-0 max-w-[1120px] gap-5 px-4 py-5 md:px-6">
    <div className="flex flex-wrap items-center gap-2">
      <AccountPageTitle accountId={id} page="账户概览" name={item.name} channel={item.trade_channel} market={item.market}
        back={{ to: '/', label: '返回所有账户' }} />
      <Link to={`/accounts/${id}/edit/signal`} className={`${signalAction} ml-auto`}>设置</Link>
    </div>
    <ErrorNotice title="账户读取失败" intent="read" error={account.error ?? config.error} />
    <ErrorNotice title="操作失败" intent="mutation" error={error} />
    <Section label="组合与定时">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 py-3 text-[14px]">
        <dt className="text-ink-3">跟随组合</dt>
        <dd>{item.portfolio_id ? <Link className="text-accent hover:underline" to={`/portfolios/${item.portfolio_id}/edit`}>{portfolioName ?? '打开组合'}</Link> : '未绑定'}</dd>
        <dt className="text-ink-3">下次计算</dt>
        <dd>{item.next_run_time ? shanghaiLabel(item.next_run_time) : item.is_started ? '尚未排程' : '已暂停'}</dd>
        <dt className="text-ink-3">通知</dt>
        <dd>{notification?.last_attempt_ok === false ? notification.last_attempt_error || '上次推送失败'
          : notification?.last_success_at ? `已推送 · ${shanghaiLabel(notification.last_success_at)}` : '暂无推送'}</dd>
      </dl>
    </Section>
    <Section label="目标持仓" action={<button type="button" className={signalAction} onClick={() => void act('target')} disabled={busy !== null || item.portfolio_id === null}>{busy === 'target' ? '计算中…' : '重新计算目标'}</button>}>
      <div className="py-3">
        <ErrorNotice intent="read" title="目标读取失败" error={targets.error} onRetry={targets.refresh} />
        {targets.data?.calculated_at ? <>
          <p className="mb-2.5 text-[13px] text-ink-3">{shanghaiLabel(targets.data.calculated_at)}</p>
          <WeightResult target={targets.data.raw_targets ?? {}} mode="quantity" quantityUnit={config.data?.quantity_unit} />
        </> : <p className="text-[14px] text-ink-3">{targets.loading ? '加载目标…' : '尚未计算'}</p>}
      </div>
    </Section>
    <Section label="资金与持仓" action={<button type="button" className={signalAction} onClick={() => void act('assets')} disabled={busy !== null}>{busy === 'assets' ? '刷新中…' : '刷新权益'}</button>}>
      <div className="grid gap-2 py-3 text-[14px]">
        <ErrorNotice intent="read" title="资产读取失败" error={assets.error} onRetry={assets.refresh} />
        {latest ? <>
          <p className="num text-ink-2">权益 {latest.assets.total_asset?.toLocaleString()} {latest.assets.currency} · 可用资金 {typeof latest.assets.available_cash === 'number' ? latest.assets.available_cash.toLocaleString() : '—'}</p>
          <p className="text-[13px] text-ink-3">更新于 {shanghaiLabel(latest.created_at)}</p>
          {positions ? positions.length === 0 ? <p className="text-ink-3">当前空仓</p> : <ul className="divide-y divide-line">
            {positions.map((row) => <li key={`${row.symbol}:${row.direction}`} className="grid grid-cols-[1fr_auto_auto] gap-4 py-2">
              <span>{row.symbol}</span><span>{row.direction}</span><span className="num">{row.volume} {config.data?.quantity_unit}</span>
            </li>)}
          </ul> : <p className="text-ink-3">未提供实际持仓</p>}
        </> : <p className="text-ink-3">{assets.loading ? '加载资产…' : '尚未刷新权益'}</p>}
      </div>
    </Section>
    <CashFlowPanel accountId={id} />
  </div>
}

function SignalSettings({ accountId, account, initial }: {
  accountId: number
  account: Account
  initial: Awaited<ReturnType<typeof getSignalConfig>>['account_config']
}) {
  const [draft, setDraft] = useState(() => configToSignalDraft(initial))
  const [portfolioId, setPortfolioId] = useState(account.portfolio_id)
  const [cron, setCron] = useState(account.cron_expr)
  const [started, setStarted] = useState(account.is_started)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [baseline, setBaseline] = useState(() => ({ draft, portfolioId, cron, started }))
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const current = { draft, portfolioId, cron, started }
  const savedQuoteCode = baseline.draft.quoteKind === 'custom' ? baseline.draft.quoteCode : initialSignalDraft.quoteCode
  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await updateAccount(accountId, { account_config: { ...signalConfig(draft) }, portfolio_id: portfolioId, cron_expr: cron.trim(), is_started: started })
      setBaseline(current)
      await refreshAccounts()
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      setBusy(false)
    }
  }
  return <div className="mx-auto grid min-w-0 max-w-[1120px] gap-5 px-4 py-5 md:px-6">
    <div className="flex flex-wrap items-center gap-2">
      <AccountPageTitle accountId={accountId} page="信号设置" name={account.name} channel={account.trade_channel} market={account.market} />
      <Link className={`${signalAction} ml-auto`} to={`/accounts/${accountId}/edit/notification`}>通知函数</Link>
    </div>
    <Section
      label="跟随组合"
      action={<button
        type="button"
        aria-pressed={portfolioId === null}
        onClick={() => setPortfolioId(null)}
        className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-[13px] text-ink-2 hover:bg-fill focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        {portfolioId === null && <Check size={14} aria-hidden />}不绑定组合
      </button>}
    ><div className="py-3"><PortfolioChoiceList selectedId={portfolioId} onSelect={setPortfolioId} /></div></Section>
    <Section label="数据来源"><div className="py-3">
      <SignalSources identity={`signal:${accountId}`} value={draft} onChange={setDraft} savedQuoteCode={savedQuoteCode} />
    </div></Section>
    <Section label="定时计算">
      <div className="grid gap-3 py-3">
        <ConnectionField label="定时表达式" kind="text" value={cron} onChange={setCron} placeholder="30 15 * * 1-5" fillablePlaceholder />
        <div className="flex items-center gap-2 text-[13px] text-ink-2">
          <Toggle on={started} onClick={() => setStarted(!started)} ariaLabel="启用定时任务" /><span>启用定时任务</span>
        </div>
      </div>
    </Section>
    <EditSaveBar changes={JSON.stringify(current) === JSON.stringify(baseline) ? [] : ['信号设置已改']} blocked={busy || (started && !cron.trim())}
      onCancel={() => { setDraft(baseline.draft); setPortfolioId(baseline.portfolioId); setCron(baseline.cron); setStarted(baseline.started); setError(null) }}
      onSave={() => void save()} error={error} />
  </div>
}

export function SignalAccountSettingsPage() {
  const { id } = useParams()
  const accountId = Number(id)
  const account = usePolling(useCallback((signal: AbortSignal) => getAccount(accountId, signal), [accountId]),
    { queryKey: `signal-settings-account:${accountId}`, intervalMs: 0 })
  const config = usePolling(useCallback((signal: AbortSignal) => getSignalConfig(accountId, signal), [accountId]),
    { queryKey: `signal-settings-config:${accountId}`, intervalMs: 0 })
  if (account.error || config.error) return <ErrorNotice title="信号设置读取失败" intent="read" error={account.error ?? config.error} />
  if (!account.data || !config.data) return <EditLoading />
  return <SignalSettings key={accountId} accountId={accountId} account={account.data} initial={config.data.account_config} />
}
