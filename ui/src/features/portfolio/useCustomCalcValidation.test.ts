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
  let options: CustomCalcValidation['contextOptions'] = []
  function Probe() {
    options = useCustomCalcValidation('return {}', market).contextOptions
    return null
  }
  renderToStaticMarkup(createElement(Probe))
  return options
}

test('账户选项名称带多空杠杆，交易提示放在分组说明', () => {
  const options = optionsFor(3.5, 2)
  expect(options[0]).toEqual({
    value: null, label: '样例数据', description: '固定样例资产，价格恒为 100，不连接真实渠道',
  })
  expect(options[1]).toEqual({
    value: 7, label: 'schwartx · 多 3.5× / 空 2×', hint: '天勤 · 期货',
    groupLabel: '真实持仓与行情 · 主动交易会产生真实委托',
  })
})

test('零杠杆保留，未设置或旧接口缺字段显示未知', () => {
  expect(optionsFor(0, 0)[1]?.label).toBe('schwartx · 多 0× / 空 0×')
  expect(optionsFor(null, null)[1]?.label).toBe('schwartx · 多 — / 空 —')
  expect(optionsFor(undefined, undefined)[1]?.label).toBe('schwartx · 多 — / 空 —')
})

test('单向渠道只显示一个仓位系数', () => {
  expect(optionsFor(1.5, 0, { showShort: false })[1]?.label).toBe('schwartx · 1.5×')
})

test('期货组合只列出期货账户，兼容市场标识和显示名', () => {
  const otherAccounts = [
    { account_id: 8, name: 'ichika', market: 'ctp', trade_channel: 'ctp', long_leverage: 3, short_leverage: 3 },
    { account_id: 9, name: 'crypto', market: 'crypto', trade_channel: 'binance' },
  ] as AccountDashboardItem[]
  for (const market of ['ctp', '期货']) {
    const options = optionsFor(2, 2, { market, otherAccounts })
    expect(options.map((option) => option.value)).toEqual([null, 7, 8])
    expect(options.slice(1).every((option) => !option.description && option.groupLabel)).toBe(true)
  }
  expect(optionsFor(2, 2, { market: 'A股', otherAccounts }).map((option) => option.value)).toEqual([null])
  expect(optionsFor(2, 2, { market: null, otherAccounts }).map((option) => option.value)).toEqual([null])
})
