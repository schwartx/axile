import { describe, expect, it } from 'bun:test'

import { defaultExecutionTimeoutForChannel, executionTimeoutError } from './executionTimeout'
import { getChannelForMarket, useChannelCatalogStore } from '@/stores/channels'

describe('defaultExecutionTimeoutForChannel', () => {
  it('优先使用运行时渠道目录，未知渠道使用中性兜底', () => {
    useChannelCatalogStore.setState({
      channels: [{
        channel: 'paper', label: '纸面交易', description: '', icon: 'P', available: true,
        missing_packages: [], install_extra: '', market: 'demo-market', currency: 'CNY',
        schedule: { kind: 'continuous' },
        calendar: null,
        units: {
          quantity_kind: 'custom', quantity_label: '', quantity_max_decimals: 6,
          price_label: '', notional_label: '',
        },
        ui: {
          account_connect_lead: '', leverage_title: '杠杆', leverage_note: '多空可分设',
          long_leverage_label: '做多杠杆', short_leverage_label: '做空杠杆', show_short_leverage: true,
        },
        defaults: {
          account_control_preset: 'default',
          long_leverage: 1, short_leverage: 1, execution_timeout: 420,
          trade_algorithm: { method: 'SINGLE-MAKER', params: {} }, empty_positions_algorithm: null,
        },
        leverage: { min: 0, max: 10, step: 0.1 },
        account_form: { fields: [], notices: [] },
        portfolio: { market_label: '测试市场', example_symbols: ['DEMO'] },
      }],
    })
    expect(defaultExecutionTimeoutForChannel('paper')).toBe('420')
    expect(getChannelForMarket('demo-market')).toBe('paper')
    expect(getChannelForMarket('测试市场')).toBe('paper')
    expect(defaultExecutionTimeoutForChannel('unknown')).toBe('300')
    useChannelCatalogStore.setState({ channels: null })
  })
})

describe('executionTimeoutError', () => {
  it('接受正整数秒，包括超过原上限的小时及天级时长', () => {
    expect(executionTimeoutError('1')).toBeNull()
    expect(executionTimeoutError('300')).toBeNull()
    expect(executionTimeoutError('540')).toBeNull()
    expect(executionTimeoutError('541')).toBeNull()
    expect(executionTimeoutError('3600')).toBeNull()
    expect(executionTimeoutError('86400')).toBeNull()
  })

  it('拒绝空值、小数和非正数', () => {
    expect(executionTimeoutError('')).toBe('请输入整数秒')
    expect(executionTimeoutError('1.5')).toBe('需为整数秒')
    expect(executionTimeoutError('0')).toContain('≥ 1')
    expect(executionTimeoutError('-1')).toContain('≥ 1')
  })

  it('编辑页可把空值解释为不修改', () => {
    expect(executionTimeoutError('', { allowEmpty: true })).toBeNull()
  })
})
