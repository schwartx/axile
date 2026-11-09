import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { useChannelCatalogStore } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import type { AccountDashboardItem, ChannelCapability } from '@/types/api'
import { useCustomCalcValidation } from './useCustomCalcValidation'

test('试跑仅列出同市场账户，兼容市场标识和显示名', () => {
  const domain = useDomainStore.getInitialState()
  const catalog = useChannelCatalogStore.getInitialState()
  const { accounts } = domain
  const { channels } = catalog
  domain.accounts = [
    { account_id: 7, name: '期货一', market: '期货', trade_channel: 'ctp' },
    { account_id: 8, name: '期货二', market: 'ctp', trade_channel: 'tq' },
    { account_id: 9, name: '股票', market: 'A股', trade_channel: 'gm' },
  ] as AccountDashboardItem[]
  catalog.channels = [
    { channel: 'ctp', label: 'CTP', market: 'ctp', portfolio: { market_label: '期货' } },
    { channel: 'gm', label: 'GM', market: 'gm', portfolio: { market_label: 'A股' } },
  ] as ChannelCapability[]

  function optionsFor(market: string | undefined) {
    let accountIds: (number | null)[] = []
    function Probe() {
      accountIds = useCustomCalcValidation('return {}', market).contextOptions.map(option => option.value)
      return null
    }
    renderToStaticMarkup(createElement(Probe))
    return accountIds
  }

  try {
    expect(optionsFor('ctp')).toEqual([null, 7, 8])
    expect(optionsFor('期货')).toEqual([null, 7, 8])
    expect(optionsFor('gm')).toEqual([null, 9])
    expect(optionsFor(undefined)).toEqual([null])
  } finally {
    domain.accounts = accounts
    catalog.channels = channels
  }
})
