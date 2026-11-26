import { useState } from 'react'
import { Check } from 'lucide-react'
import { useOutletContext, useParams } from 'react-router'
import type { AccountEditContext } from '@/features/account/AccountEditGuard'
import { EditLoading, EditSaveBar, Row, Section, Toggle } from '@/features/account/editUi'
import { AccountPageTitle } from '@/features/account/pageHead'
import { PortfolioChoiceList } from '@/features/portfolio/PortfolioChoiceList'
import { SIGNAL_CHANNEL } from '@/features/signal/signalData'
import { updateAccount, type AccountUpdatePayload } from '@/lib/api/accounts'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'

export function AccountEditPortfolioPage() {
  const { id } = useParams()
  return <PortfolioSettings key={id} accountId={Number(id)} />
}

function PortfolioSettings({ accountId }: { accountId: number }) {
  const { data: account, refresh } = useOutletContext<AccountEditContext>()
  const portfolios = useDomainStore(s => s.portfolios)
  const portfoliosError = useDomainStore(s => s.portfoliosError)
  const refreshAccounts = useDomainStore(s => s.refreshAccounts)
  const catalogLoading = useChannelCatalogStore(s => s.loading)
  const catalogError = useChannelCatalogStore(s => s.error)
  const channel = useChannelDescriptor(account.trade_channel)
  const toast = useToastStore(s => s.toast)
  const [portfolioId, setPortfolioId] = useState(account.portfolio_id)
  const [writeEmpty, setWriteEmpty] = useState(Boolean(account.write_empty_record))
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<Error | null>(null)
  const signal = account.trade_channel === SIGNAL_CHANNEL
  const title = signal ? '绑定组合' : '组合执行'
  const pageChrome = <div className="flex flex-wrap items-center gap-3">
    <AccountPageTitle accountId={accountId} page={title} name={account.name} channel={account.trade_channel} market={account.market} />
  </div>

  if (!channel && catalogLoading) return <section className="pb-24">{pageChrome}<EditLoading bare /></section>

  const patch: AccountUpdatePayload = {}
  const changes: string[] = []
  if (portfolioId !== account.portfolio_id) {
    const portfolioName = (value: number | null) => value == null ? '未绑定' : portfolios?.find(p => p.id === value)?.name ?? `#${value}`
    patch.portfolio_id = portfolioId
    changes.push(`组合 ${portfolioName(account.portfolio_id)}→${portfolioName(portfolioId)}`)
  }
  if (!signal && writeEmpty !== Boolean(account.write_empty_record)) {
    patch.write_empty_record = writeEmpty ? 1 : 0
    changes.push(`空仓记录 ${writeEmpty ? '关→开' : '开→关'}`)
  }
  const save = async () => {
    if (saving || !changes.length) return
    setSaveError(null)
    setSaving(true)
    try {
      const updated = await updateAccount(accountId, patch)
      setPortfolioId(updated.portfolio_id)
      setWriteEmpty(Boolean(updated.write_empty_record))
      toast('账户已更新')
      void refreshAccounts()
      void refresh()
    } catch (error) {
      setSaveError(error instanceof Error ? error : new Error(String(error)))
    } finally {
      setSaving(false)
    }
  }
  const choosePortfolio = (value: number | null) => { setPortfolioId(value); setSaveError(null) }

  return <section>
    <fieldset disabled={saving}>
      {pageChrome}
      <Section label={title}>
        {!signal && <Row label="空仓写记录" hint="审计留痕">
          <Toggle ariaLabel="空仓写记录" on={writeEmpty} onClick={() => { setWriteEmpty(!writeEmpty); setSaveError(null) }} />
        </Row>}
        <div className="mt-3 min-w-0 md:col-span-2">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="text-[14px] text-ink-2">跟随组合</h2>
            <button type="button" aria-pressed={portfolioId == null}
              disabled={portfolios == null || Boolean(portfoliosError)} onClick={() => choosePortfolio(null)}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-[13px] text-ink-2 hover:bg-fill focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-45">
              {portfolioId == null && <Check size={14} aria-hidden />}不绑定组合
            </button>
          </div>
          <PortfolioChoiceList market={account.market} selectedId={portfolioId} onSelect={choosePortfolio} />
        </div>
      </Section>
    </fieldset>
    <EditSaveBar changes={changes} blocked={portfolios == null || Boolean(portfoliosError || catalogError)} saving={saving}
      onCancel={() => { if (!saving) { choosePortfolio(account.portfolio_id); setWriteEmpty(Boolean(account.write_empty_record)) } }}
      onSave={() => void save()} error={saveError} />
  </section>
}
