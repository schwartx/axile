import { SIGNAL_CHANNEL } from '@/features/signal/signalData'
import { Settings } from 'lucide-react'
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Chip } from '@/components/ui/Card'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { OverflowText } from '@/components/ui/OverflowText'
import { Select } from '@/components/ui/Select'
import { usePythonSave } from '@/components/ui/pythonWorkbenchState'
import { LeverageInput } from '@/features/account/LeverageInput'
import { AccountConnectionStatus } from '@/features/account/AccountConnectionStatus'
import { describeLeverage, writeAccountConfigSummary } from '@/features/account/configSummary'
import { connectionLabel } from '@/features/account/connectionProblem'
import { leverageError, leverageValue } from '@/features/account/leverage'
import { useConnectionFeedback } from '@/features/account/useConnectionFeedback'
import { channelLabel } from '@/features/dashboard/display'
import { updateAccount } from '@/lib/api/accounts'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import { useSimulationAccounts } from '@/stores/simulationAccounts'
import { SimulationAccountModal } from '@/features/portfolio/SimulationAccountModal'
import { useLiveConnection } from '@/stores/liveConnection'
import type { AccountDashboardItem, ChannelCapability, SimulationAccount, TrialLeverageOverrides } from '@/types/api'

const textAction = 'min-w-0 cursor-pointer rounded px-1.5 py-1 text-left text-[12.5px] text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-45'

/** 原位编辑真实账户的试跑杠杆，只有显式保存才修改账户。 */
export function AccountLeverageControl({ account, disabled, onLeverageChange }: {
  account: AccountDashboardItem | null
  disabled: boolean
  onLeverageChange: (id: number | null, overrides: TrialLeverageOverrides | null, invalid: boolean) => void
}) {
  const channel = useChannelDescriptor(account?.trade_channel)
  const showShort = channel?.ui.show_short_leverage ?? true
  const ownerId = account?.account_id ?? null
  const id = useId()
  const [draft, setDraft] = useState<{ long: string; short: string } | null>(null)
  const saveState = usePythonSave()
  const pendingDraft = useRef<{ long: string; short: string } | null>(null)
  useEffect(() => () => onLeverageChange(ownerId, null, false), [ownerId, onLeverageChange])
  const baselineLong = account?.long_leverage ?? channel?.defaults.long_leverage ?? 1
  const baselineShort = account?.short_leverage ?? channel?.defaults.short_leverage ?? 1
  const values = draft ?? { long: String(baselineLong ?? ''), short: String(baselineShort ?? '') }
  const longError = leverageError(values.long, channel?.leverage)
  const shortError = showShort ? leverageError(values.short, channel?.leverage) : null
  const changed = useCallback((next: { long: string; short: string }) =>
    leverageValue(next.long, channel?.leverage) !== baselineLong
    || (showShort && leverageValue(next.short, channel?.leverage) !== baselineShort),
  [baselineLong, baselineShort, channel?.leverage, showShort])
  const dirty = draft !== null && changed(draft)
  useEffect(() => {
    if (pendingDraft.current && changed(pendingDraft.current)) return
    pendingDraft.current = null
    setDraft(null)
    onLeverageChange(ownerId, null, false)
  }, [changed, ownerId, onLeverageChange])
  const change = (next: { long: string; short: string }) => {
    setDraft(next)
    saveState.clearError()
    pendingDraft.current = changed(next) ? next : null
    const long = leverageValue(next.long, channel?.leverage)
    const short = showShort ? leverageValue(next.short, channel?.leverage) : null
    const invalid = long === null || (showShort && short === null)
    onLeverageChange(ownerId,
      !invalid && pendingDraft.current ? { long_leverage: long!, ...(showShort ? { short_leverage: short! } : {}) } : null, invalid)
  }
  const cancel = () => {
    if (saveState.saving) return
    pendingDraft.current = null
    setDraft(null)
    saveState.clearError()
    onLeverageChange(ownerId, null, false)
  }
  const save = async () => {
    if (!account || !dirty || !channel || longError || shortError) return
    const long = leverageValue(values.long, channel.leverage)!
    const short = showShort ? leverageValue(values.short, channel.leverage)! : baselineShort
    const patch = {
      ...(long !== baselineLong ? { long_leverage: long } : {}),
      ...(showShort && short !== baselineShort ? { short_leverage: short } : {}),
    }
    await saveState.save(true, async () => {
      const updated = await updateAccount(account.account_id, patch)
      writeAccountConfigSummary(account.account_id, updated, { showShortLeverage: showShort })
      useDomainStore.setState((state) => ({ accounts: state.accounts?.map((item) => item.account_id === account.account_id
        ? { ...item, long_leverage: updated.long_leverage, short_leverage: updated.short_leverage } : item) ?? null }))
      void useDomainStore.getState().refreshAccounts()
      pendingDraft.current = null
      setDraft(null)
      onLeverageChange(ownerId, null, false)
    })
  }
  return <div className="w-full min-w-0">
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <fieldset disabled={disabled || (!!account && !channel) || saveState.saving} className="flex flex-wrap gap-2 text-[12px] text-ink-2">
        <div className="flex max-w-full items-center gap-1">
          <label htmlFor={`${id}-long`} title={channel?.ui.long_leverage_label}>{showShort ? '多' : '系数'}</label>
          <LeverageInput compact id={`${id}-long`} value={values.long} limits={channel?.leverage} invalid={!!longError}
            describedBy={longError ? `${id}-error` : undefined} onChange={(long) => change({ ...values, long })} />
        </div>
        {showShort && <div className="flex max-w-full items-center gap-1">
          <label htmlFor={`${id}-short`} title={channel?.ui.short_leverage_label}>空</label>
          <LeverageInput compact id={`${id}-short`} value={values.short} limits={channel?.leverage} invalid={!!shortError}
            describedBy={shortError ? `${id}-error` : undefined} onChange={(short) => change({ ...values, short })} />
        </div>}
      </fieldset>
      <div inert={!dirty} aria-hidden={!dirty}
        className={`ml-auto grid min-w-0 max-w-full shrink-0 transition-[grid-template-columns] duration-200 motion-reduce:transition-none ${dirty ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]'}`}>
        <div className="min-w-0 overflow-hidden">
          <div className="flex h-7 items-center gap-1 whitespace-nowrap">
            {account && <button type="button" className={textAction} disabled={disabled || saveState.saving || !channel || !!longError || !!shortError}
              onClick={() => void save()}><InkRewrite text={saveState.saving ? '保存中…' : '保存到账户'} tone="label" /></button>}
            <button type="button" className={textAction} disabled={disabled || saveState.saving} onClick={cancel}>还原</button>
          </div>
        </div>
      </div>
    </div>
    <div inert={!dirty} aria-hidden={!dirty}
      className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${dirty ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden">
        {(longError || shortError) && <p id={`${id}-error`} role="status" className="text-[12px] text-warn">{longError ?? shortError}</p>}
        <ErrorNotice title="杠杆保存失败" intent="mutation" error={saveState.error} variant="compact" />
      </div>
    </div>
  </div>
}

/** 页面只显示当前账户；全部候选收在有搜索和滚动上限的选择浮层中。 */
export function PortfolioTrialAccounts({ accounts, accountId, portfolioId, onSelect, onLeverageChange, disabled, leverageEditing = false, simulationChannels = [], simulationChannel, simulationAccount = null, onSimulationSelect }: {
  accounts: AccountDashboardItem[]
  accountId: number | null
  portfolioId: number
  onSelect: (id: number | null) => void
  onLeverageChange: (id: number | null, overrides: TrialLeverageOverrides | null, invalid: boolean) => void
  disabled: boolean
  leverageEditing?: boolean
  simulationChannels?: ChannelCapability[]
  simulationChannel?: ChannelCapability
  simulationAccount?: SimulationAccount | null
  onSimulationSelect?: (channel: string) => void
}) {
  const [configOpen, setConfigOpen] = useState(false)
  const saveSimulation = useSimulationAccounts((state) => state.save)
  const selected = accounts.find((account) => account.account_id === accountId)
  const usesTrading = selected?.trade_channel !== SIGNAL_CHANNEL
  const channels = useChannelCatalogStore((state) => state.channels)
  const frames = useLiveConnection((state) => state.frames)
  const synced = useLiveConnection((state) => state.connected)
  const feedback = useConnectionFeedback(accountId ?? -1)
  const error = useDomainStore((state) => state.accountsError)
  const loaded = useDomainStore((state) => state.accounts !== null)
  const refresh = useDomainStore((state) => state.refreshAccounts)
  const options = [
    ...simulationChannels.map((channel) => ({ value: `simulation:${channel.channel}`, label: `模拟 · ${channel.label}`, groupLabel: '模拟' })),
    ...accounts.map((account) => ({
      value: account.account_id,
      label: account.trade_channel === SIGNAL_CHANNEL ? account.name : `${account.name} · ${describeLeverage(account.long_leverage ?? null, account.short_leverage ?? null,
        channels?.find((channel) => channel.channel === account.trade_channel)?.ui.show_short_leverage ?? true)}`,
      groupLabel: '账户',
      hint: `${synced && frames[account.account_id] ? connectionLabel(frames[account.account_id]) : '状态待确认'}${account.portfolio_id === portfolioId ? ' · 跟随本组合' : ''}`,
    })),
  ]
  return <section aria-label="试跑账户" className="mt-4 min-w-0">
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <div className="flex min-w-0 max-w-full items-center gap-1.5">
        <Select<number | string> value={selected?.account_id ?? `simulation:${simulationChannel?.channel ?? ''}`} options={options}
          onChange={(value) => typeof value === 'number' ? onSelect(value) : onSimulationSelect?.(value.slice('simulation:'.length))} searchable disabled={disabled || leverageEditing}
          ariaLabel="试跑账户" appearance="inline" className="min-w-0 max-w-full px-1.5 py-1 text-[14px]"
          selectedValue={<OverflowText text={selected?.name ?? (simulationChannel ? `模拟 · ${simulationChannel.label}` : '正在加载模拟账户…')} />} />
        {selected && <Chip className="max-w-48 shrink-0"><OverflowText text={channelLabel(selected.trade_channel, '')} /></Chip>}
      </div>
      {selected && usesTrading && <fieldset disabled={disabled} className="ml-auto min-w-0 max-w-full">
        <AccountConnectionStatus accountId={selected.account_id} />
      </fieldset>}
      {!selected && simulationChannel && simulationAccount && <button type="button" disabled={disabled} className={`${textAction} ml-auto inline-flex shrink-0 items-center gap-1.5`} onClick={() => setConfigOpen(true)}><Settings size={14} aria-hidden />配置</button>}
    </div>
    {selected ? usesTrading && <div className="mt-1 flex min-h-8 min-w-0 items-start">
      <AccountLeverageControl key={selected.account_id} account={selected} disabled={disabled} onLeverageChange={onLeverageChange} />
    </div> : simulationChannel && simulationAccount && <div className="mt-1 flex min-h-8 flex-wrap items-center gap-2 px-1.5 text-xs text-ink-3">
      <span className="tabular-nums">权益 {simulationAccount.account.total_asset.toLocaleString()} {simulationAccount.account.currency} · 持仓 {simulationAccount.account.positions.length} · 行情 {simulationAccount.quotes.length}</span>
    </div>}
    {configOpen && simulationChannel && simulationAccount && <SimulationAccountModal channel={simulationChannel} value={simulationAccount}
      sources={simulationChannel.independent_market_data ? accounts.filter((account) => !account.archived_at && account.trade_channel === simulationChannel.channel) : []}
      onSave={(value) => saveSimulation(simulationChannel.channel, value)} onClose={() => setConfigOpen(false)} />}
    <ErrorNotice title="账户列表加载失败" intent="read" error={error} hasData={loaded} variant="compact" onRetry={refresh} />
    <ErrorNotice title="连接操作失败" intent="mutation" error={selected ? feedback.error : null} contextKey={accountId ?? 'sample'} variant="compact" networkFeedback="global-only" />
  </section>
}
