import { describe, expect, it } from 'bun:test'

import type { ChannelCapability } from '@/types/api'
import { portfolioMarketOptions, portfolioQuantityUnit, portfolioTemplate } from './portfolioMarkets'

function channel(
  market: string,
  label: string,
  symbols: string[],
  available = true,
): ChannelCapability {
  return {
    channel: `${market}-${label}`,
    label,
    description: label,
    icon: 'landmark',
    available,
    missing_packages: available ? [] : ['vendor-sdk'],
    install_extra: null,
    market,
    schedule: { kind: 'continuous' },
    calendar: null,
    currency: 'CNY',
    units: {
      quantity_kind: 'custom',
      quantity_label: '',
      quantity_max_decimals: 6,
      price_label: '',
      notional_label: '',
    },
    ui: {
      account_connect_lead: '',
      leverage_title: '杠杆',
      leverage_note: '',
      long_leverage_label: '做多杠杆',
      short_leverage_label: '做空杠杆',
      show_short_leverage: true,
    },
    defaults: {
      account_control_preset: 'default',
      long_leverage: 1,
      short_leverage: 1,
      execution_timeout: 60,
      trade_algorithm: { method: 'DEMO', params: {} },
      empty_positions_algorithm: null,
    },
    leverage: { min: 0, max: 10, step: 1 },
    account_form: { fields: [], notices: [] },
    portfolio: { market_label: label, example_symbols: symbols },
  }
}

describe('portfolio market presets', () => {
  const futuresChannel = channel('ctp', '期货', ['rb2610', 'ag2612'])
  const stocksChannel = channel('ashare', 'A股', ['600000.SH', '000001.SZ'])
  const options = portfolioMarketOptions([futuresChannel, stocksChannel])
  const futures = options[0]!

  it('按账户渠道或组合市场解析单位，保留渠道声明的空单位', () => {
    const contracts = { ...futuresChannel, units: { ...futuresChannel.units, quantity_label: '手' } }
    const shares = { ...stocksChannel, units: { ...stocksChannel.units, quantity_label: '股' } }
    const unnamed = { ...contracts, channel: 'custom', units: { ...contracts.units, quantity_label: '' } }
    const catalog = [contracts, shares, unnamed]
    expect(portfolioQuantityUnit(catalog, 'ctp')).toBe('手')
    expect(portfolioQuantityUnit(catalog, '期货')).toBe('手')
    expect(portfolioQuantityUnit(catalog, 'A股')).toBe('股')
    expect(portfolioQuantityUnit(catalog, '期货', shares.channel)).toBe('股')
    expect(portfolioQuantityUnit(catalog, '期货', 'custom')).toBe('')
    expect(portfolioQuantityUnit(catalog, '未知市场')).toBe('')
    expect(portfolioQuantityUnit([], '期货')).toBe('')
  })

  it('deduplicates channels in stable market order', () => {
    const duplicate = channel('ctp', '另一个期货渠道', ['cu2610'])
    expect(portfolioMarketOptions([futuresChannel, duplicate, stocksChannel])).toEqual([
      { value: 'ctp', label: '期货', exampleSymbols: ['rb2610', 'ag2612'] },
      { value: 'ashare', label: 'A股', exampleSymbols: ['600000.SH', '000001.SZ'] },
    ])
  })

  it('adds a market only when its channel plugin is registered', () => {
    expect(portfolioMarketOptions([futuresChannel, stocksChannel]).map((option) => option.value)).toEqual([
      'ctp',
      'ashare',
    ])
    const external = channel('external-market', '外部市场', ['PLUGIN-SYMBOL'])
    expect(portfolioMarketOptions([futuresChannel, stocksChannel, external]).map((option) => option.value)).toEqual([
      'ctp',
      'ashare',
      'external-market',
    ])
  })

  it('keeps registered markets whose optional dependencies are unavailable', () => {
    const unavailable = channel('external', '外部市场', ['EXT-1'], false)
    expect(portfolioMarketOptions([unavailable])).toEqual([
      { value: 'external', label: '外部市场', exampleSymbols: ['EXT-1'] },
    ])
  })

  it('builds an equal-weight runnable template from example symbols', () => {
    expect(portfolioTemplate(futures)).toBe(
      'from axile.server.context import Context\n\ndef calculate_portfolio(context: Context) -> dict[str, float]:\n    # 返回 {品种: 目标权重}\n    return {"rb2610": 0.5, "ag2612": 0.5}',
    )
  })
})
