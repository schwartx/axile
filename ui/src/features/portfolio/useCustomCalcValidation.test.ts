import { afterEach, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { useChannelCatalogStore } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import type { AccountDashboardItem, ChannelCapability } from '@/types/api'
import { useCustomCalcValidation, type CustomCalcValidation } from './useCustomCalcValidation'

const initialDomain = { ...useDomainStore.getInitialState() }
const initialChannels = { ...useChannelCatalogStore.getInitialState() }

afterEach(() => {
  Object.assign(useDomainStore.getInitialState(), initialDomain)
  Object.assign(useChannelCatalogStore.getInitialState(), initialChannels)
  useDomainStore.setState(initialDomain)
  useChannelCatalogStore.setState(initialChannels)
})

function optionsFor(long: number | null | undefined, short: number | null | undefined, {
  showShort = true, market = '期货', otherAccounts = [],
}: { showShort?: boolean; market?: string | null; otherAccounts?: AccountDashboardItem[] } = {}) {
  useDomainStore.setState({ accounts: [{
    account_id: 7, name: 'schwartx', market: '期货', trade_channel: 'ctp',
    long_leverage: long, short_leverage: short,
  } as AccountDashboardItem, ...otherAccounts] })
  useChannelCatalogStore.setState({ channels: [{
    channel: 'ctp', label: '天勤', market: 'ctp', portfolio: { market_label: '期货' },
    ui: { show_short_leverage: showShort },
  } as ChannelCapability] })
  Object.assign(useDomainStore.getInitialState(), useDomainStore.getState())
  Object.assign(useChannelCatalogStore.getInitialState(), useChannelCatalogStore.getState())
  let accounts: CustomCalcValidation['accounts'] = []
  function Probe() {
    accounts = useCustomCalcValidation('return {}', market).accounts
    return null
  }
  renderToStaticMarkup(createElement(Probe))
  return accounts
}

test('期货组合只列出期货账户，兼容市场标识和显示名', () => {
  const otherAccounts = [
    { account_id: 8, name: 'ichika', market: 'ctp', trade_channel: 'ctp', long_leverage: 3, short_leverage: 3 },
    { account_id: 9, name: 'crypto', market: 'crypto', trade_channel: 'binance' },
  ] as AccountDashboardItem[]
  for (const market of ['ctp', '期货']) {
    const options = optionsFor(2, 2, { market, otherAccounts })
    expect(options.map((account) => account.account_id)).toEqual([7, 8])
  }
  expect(optionsFor(2, 2, { market: 'A股', otherAccounts }).map((account) => account.account_id)).toEqual([])
  expect(optionsFor(2, 2, { market: null, otherAccounts }).map((account) => account.account_id)).toEqual([])
})
