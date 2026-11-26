import { useCallback, useState, type ReactNode } from 'react'
import { Navigate, Outlet, useOutletContext, useParams } from 'react-router'
import type { AccountEditContext } from '@/features/account/AccountEditGuard'
import { EditError, EditLoading, EditSaveBar } from '@/features/account/editUi'
import { AccountPageTitle } from '@/features/account/pageHead'
import { portfolioMarketDescriptor } from '@/features/portfolio/portfolioMarkets'
import { SignalAssetsSetup } from '@/features/signal/SignalAssetsSetup'
import { SignalSources } from '@/features/signal/SignalSources'
import { getSignalConfig } from '@/features/signal/signalApi'
import {
  configToSignalDraft, SIGNAL_CHANNEL, signalAccountConfig, signalAccountErrors,
  signalQuoteConfig, signalQuoteErrors, type SignalConfig,
} from '@/features/signal/signalData'
import { updateAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useChannelCatalogStore } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import type { Account, ChannelCapability } from '@/types/api'

interface SignalDataContext {
  account: Account
  id: number
  market: ChannelCapability | undefined
  config: ReturnType<typeof usePolling<Awaited<ReturnType<typeof getSignalConfig>>>>
}

export function SignalAccountDataLayout() {
  const { data: account } = useOutletContext<AccountEditContext>()
  const id = Number(useParams().id)
  const config = usePolling(useCallback((signal: AbortSignal) => getSignalConfig(id, signal), [id]),
    { queryKey: `signal-settings:${id}`, intervalMs: 0, enabled: account.trade_channel === SIGNAL_CHANNEL })
  const channels = useChannelCatalogStore((state) => state.channels)
  const market = portfolioMarketDescriptor(channels ?? [], account.market)
  if (account.trade_channel !== SIGNAL_CHANNEL) return <Navigate to={`/accounts/${id}`} replace />
  return <Outlet context={{ account, id, market, config } satisfies SignalDataContext} />
}

export function SignalAccountDataPage({ section }: { section: 'quotes' | 'assets' }) {
  const { account, id, market, config } = useOutletContext<SignalDataContext>()
  const channels = useChannelCatalogStore((state) => state.channels)
  const catalogError = useChannelCatalogStore((state) => state.error)
  const refreshCatalog = useChannelCatalogStore((state) => state.refresh)
  const title = section === 'quotes' ? '行情设置' : '资金与持仓'
  const heading = <div className="flex flex-wrap items-center gap-2">
    <AccountPageTitle accountId={id} page={title} name={account.name} channel={account.trade_channel} market={account.market} />
  </div>
  if (config.data && market) return <SignalDataEditor key={section} id={id} account={account} section={section} market={market}
    initial={config.data.account_config} onSaved={config.refresh} heading={heading} />
  return <section className="px-4 py-5 md:px-5">
    {heading}
    {config.error ? <EditError error={config.error} onRetry={config.refresh} />
      : !market && (catalogError || channels)
        ? <EditError error={catalogError ?? new Error('所选市场未注册')} onRetry={refreshCatalog} />
        : <EditLoading bare />}
  </section>
}

function SignalDataEditor({ id, account, section, market, initial, onSaved, heading }: {
  id: number
  account: Account
  section: 'quotes' | 'assets'
  market: ChannelCapability
  initial: SignalConfig
  onSaved: () => Promise<void>
  heading: ReactNode
}) {
  const [baseline, setBaseline] = useState(() => ({
    ...configToSignalDraft(initial), currency: initial.manual_assets?.currency ?? market.currency,
  }))
  const [draft, setDraft] = useState(baseline)
  const [showErrors, setShowErrors] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const quotes = section === 'quotes'
  const title = quotes ? '行情设置' : '资金与持仓'
  const quoteErrors = signalQuoteErrors(draft)
  const assetErrors = signalAccountErrors(draft)
  const valid = !Object.values(quotes ? quoteErrors : assetErrors).some(Boolean)
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline)
  const validate = () => { setShowErrors(true); return valid }
  const update = (next: typeof draft) => {
    setDraft(next)
    setError(null)
    if (draft.mode !== next.mode || draft.quoteKind !== next.quoteKind) setShowErrors(false)
  }
  const save = async () => {
    if (saving || !dirty || !validate()) return
    setSaving(true)
    setError(null)
    try {
      const account_config = quotes ? { quote_source: signalQuoteConfig(draft) } : signalAccountConfig(draft)
      await updateAccount(id, { account_config })
      setBaseline(draft)
      await onSaved()
      void refreshAccounts()
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      setSaving(false)
    }
  }
  const footer = <EditSaveBar layout="inline" changes={dirty ? [`${title}已改`] : []} saving={saving}
    onCancel={() => { setDraft(baseline); setShowErrors(false); setError(null) }}
    onSave={() => void save()} error={error} />
  return <section className="flex h-full min-h-0 flex-col px-4 py-5 md:px-5">
    {quotes ? <SignalSources market={market.market ?? ''} exampleSymbols={market.portfolio?.example_symbols}
      quoteHeading={heading} quoteFooter={footer} quoteErrors={showErrors ? quoteErrors : undefined} onValidateQuotes={validate}
      value={draft} savedQuoteCode={baseline.quoteCode} onChange={update} identity={`signal:${id}`} />
      : <SignalAssetsSetup value={draft} errors={assetErrors} showErrors={showErrors} name={account.name}
        market={account.market} heading={heading} footer={footer} identity={`signal:${id}:account`} savedCode={baseline.accountCode}
        onChange={(patch) => update({ ...draft, ...patch })} onMissingCode={() => setShowErrors(true)} />}
  </section>
}
