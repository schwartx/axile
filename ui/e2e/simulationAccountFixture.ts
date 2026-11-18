import type { ChannelCapability, SimulationAccount } from '../src/types/api'

const preset: SimulationAccount = {
  account: { total_asset: 1000000, available_cash: 800000, market_value: 200000, currency: 'CNY', source: 'simulation', positions: [
    { symbol: 'SHFE.rb2701', direction: '多头', volume: 2, available_volume: 2, market_value: 70000, avg_price: 3500 },
  ] },
  quotes: [{ symbol: 'SHFE.rb2701', last_price: 3500, volume: 1000, bids: [[3499, 10]], asks: [[3501, 10]], timestamp: 1794873600000, update_time: '2026-11-17T10:00:00+08:00', extra: { volume_multiple: 10, price_tick: 1 } }],
  long_leverage: 1, short_leverage: 1,
}

/** 仅浏览器验收使用的离线渠道，不访问交易凭据或生产账户。 */
export const simulationChannels = ['ctp', 'tq'].map((channel) => ({
  channel, label: channel.toUpperCase(), market: 'ctp', available: false, independent_market_data: channel === 'ctp',
  portfolio: { market_label: '期货', example_symbols: ['SHFE.rb2701'] },
  units: { quantity_kind: 'contract', quantity_label: '手', quantity_max_decimals: 0 },
  currency: 'CNY', leverage: { min: 0, max: 10, step: 0.5 },
  defaults: { long_leverage: 1, short_leverage: 1 },
  ui: { show_short_leverage: true, long_leverage_label: '做多杠杆', short_leverage_label: '做空杠杆' },
  simulation_account: structuredClone(preset),
})) as ChannelCapability[]
