import type { Position } from '@/types/api'
import { displayCurrencyUnit } from '@/lib/format'

type Attributes = Record<string, unknown>
export interface PositionColumn { key: string; label: string; pnl?: boolean; quantity?: boolean }

export function recordObject(value: unknown): Attributes {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Attributes : {}
}

export function positionAttributes(position: Position): Attributes {
  // 旧快照的 avg_price 含占位零和推算价格，展示只认明确保留的原生字段。
  return recordObject(recordObject(position.extra).position_attributes)
}

export function positionNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) < Number.MAX_VALUE ? value : null
}

export function positionText(value: unknown): string {
  const number = positionNumber(value)
  if (number == null) return '—'
  return number !== 0 && Math.abs(number) < 1e-12 ? String(number) : number.toLocaleString('zh-CN', { maximumFractionDigits: 12 })
}

/** 价格、金额及盈亏统一保留两位小数；缺失值仍显示破折号。 */
export function positionDecimalText(value: unknown): string {
  const number = positionNumber(value)
  return number == null ? '—' : number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 持仓金额使用中文单位；只改显示名称，不换算金额。 */
export function positionCurrencyUnit(currency: unknown): string {
  if (typeof currency !== 'string' || !currency.trim()) return ''
  const code = currency.trim().toUpperCase()
  if (code === 'CNY') return displayCurrencyUnit(code)
  return ({ USD: '美元', HKD: '港元', EUR: '欧元', JPY: '日元', GBP: '英镑', CHF: '瑞郎', AUD: '澳元', CAD: '加元' } as Record<string, string>)[code] ?? code
}

const columns: Record<string, PositionColumn[]> = {
  ctp: [
    { key: 'today_yesterday', label: '今仓 / 昨仓' }, { key: 'position_price', label: '持仓均价' }, { key: 'position_profit', label: '持仓盈亏', pnl: true },
    { key: 'margin', label: '占用保证金' }, { key: 'open_cost', label: '开仓成本' }, { key: 'position_cost', label: '持仓成本' },
    { key: 'frozen', label: '冻结数量', quantity: true }, { key: 'close_profit', label: '平仓盈亏', pnl: true }, { key: 'commission', label: '手续费' },
  ],
  tq: [
    { key: 'today_yesterday', label: '今仓 / 昨仓' }, { key: 'open_price', label: '开仓均价' }, { key: 'float_profit', label: '浮动盈亏', pnl: true },
    { key: 'position_price', label: '持仓均价' }, { key: 'position_profit', label: '持仓盈亏', pnl: true }, { key: 'margin', label: '占用保证金' },
    { key: 'open_cost', label: '开仓成本' }, { key: 'position_cost', label: '持仓成本' }, { key: 'frozen', label: '冻结数量', quantity: true },
  ],
  gm: [
    { key: 'position_price', label: '持仓均价' }, { key: 'float_profit', label: '浮动盈亏', pnl: true }, { key: 'last_price', label: '当前价格' },
    { key: 'position_cost', label: '持仓成本' }, { key: 'amount', label: '持仓额' }, { key: 'today_volume', label: '今仓', quantity: true },
    { key: 'frozen', label: '冻结数量', quantity: true }, { key: 'diluted_price', label: '摊薄均价' }, { key: 'diluted_profit', label: '摊薄口径盈亏', pnl: true },
    { key: 'open_price', label: '开仓均价' }, { key: 'open_profit', label: '开仓口径盈亏', pnl: true },
  ],
}

export const positionColumns = (channel?: string): PositionColumn[] => columns[channel ?? ''] ?? []

export function positionColumnText(position: Position, column: PositionColumn, channel?: string): string {
  const attrs = positionAttributes(position)
  if (column.key === 'today_yesterday') {
    const extra = recordObject(position.extra)
    const yesterday = channel === 'ctp' && attrs.today_volume != null
      ? extra[position.direction === '多头' ? 'long_yd' : 'short_yd'] : attrs.yesterday_volume
    return `${positionText(attrs.today_volume)} / ${positionText(yesterday)}`
  }
  const raw = channel === 'ctp' && column.key === 'frozen' ? attrs[positionDirection(position) === '多头' ? 'long_frozen' : 'short_frozen'] : attrs[column.key]
  return column.quantity ? positionText(raw) : positionDecimalText(raw)
}

export function positionDirection(position: Position): string {
  return ({ 多头: '多头', 空头: '空头', 净: '净持仓', LONG: '多头', SHORT: '空头', long: '多头', short: '空头' } as Record<string, string>)[position.direction ?? ''] ?? '—'
}

export function positionCommonNumber(position: Position, key: 'volume' | 'available_volume' | 'market_value', channel?: string): number | null {
  const attrs = positionAttributes(position)
  if (channel === 'gm') {
    const field = key === 'available_volume' ? 'available_now' : key
    return positionNumber(attrs[field])
  }
  if (key === 'available_volume' && channel === 'tq' && positionNumber(attrs.frozen) == null) return null
  if (key === 'available_volume' && channel === 'ctp' && positionNumber(attrs[positionDirection(position) === '多头' ? 'long_frozen' : 'short_frozen']) == null) return null
  return positionNumber(position[key])
}

const detailLabels: Record<string, string> = {
  today_volume: '今仓', yesterday_volume: '昨仓', previous_volume: '上日持仓（昨结）', open_price: '开仓均价', position_price: '持仓均价',
  diluted_price: '摊薄均价', open_cost: '开仓成本', position_cost: '持仓成本', amount: '持仓额',
  float_profit: '浮动盈亏', position_profit: '持仓盈亏', unrealized_pnl: '未实现盈亏', open_profit: '开仓口径盈亏', diluted_profit: '摊薄口径盈亏',
  margin: '占用保证金', exchange_margin: '交易所保证金', frozen_margin: '冻结保证金', frozen_cash: '冻结资金', frozen_commission: '冻结手续费',
  commission: '手续费', frozen: '冻结数量', frozen_today: '冻结今仓', frozen_yesterday: '冻结昨仓', long_frozen: '多头冻结', short_frozen: '空头冻结',
  available: '非挂单冻结数量', available_today: '非挂单冻结今仓', available_now: '当前可平数量',
  settlement_price: '本次结算价', previous_settlement_price: '上次结算价', close_profit: '平仓盈亏', close_profit_by_date: '逐日盯市平仓盈亏', close_profit_by_trade: '逐笔对冲平仓盈亏',
  margin_rate_money: '保证金率（按金额）', margin_rate_volume: '保证金率（按手数）',
  combination_volume: '组合持仓', comb_long_frozen: '组合多头冻结', comb_short_frozen: '组合空头冻结', strike_frozen: '执行冻结', strike_frozen_amount: '执行冻结金额', abandon_frozen: '放弃执行冻结',
  open_volume: '开仓量', close_volume: '平仓量', open_amount: '开仓金额', close_amount: '平仓金额', tas_volume: 'TAS 持仓', tas_cost: 'TAS 持仓成本',
  initial_margin: '初始保证金', maintenance_margin: '维持保证金', isolated_margin: '逐仓保证金', isolated_wallet: '逐仓钱包余额',
  position_initial_margin: '持仓初始保证金', open_order_initial_margin: '挂单初始保证金', risk_updated_at: '风险数据更新时间戳（毫秒）',
  native_notional: '原生名义价值', native_updated_at: '持仓更新时间戳（毫秒）', position_side: '原生持仓模式',
  liquidation_price: '强平价', break_even_price: '保本价', mark_price: '标记价', margin_asset: '保证金币种', adl: 'ADL 排名',
  option_market_value: '期权市值', exchange_id: '交易所', trading_day: '交易日', native_symbol: '原生合约代码',
  last_price: '当前价格', last_trade_price: '最近成交价', last_trade_volume: '最近成交量',
  created_at: '建仓时间', updated_at: '仓位变更时间', change_event_id: '仓位变更事件', change_reason: '仓位变更原因代码',
  credit_sellable_volume: '可卖担保品数量',
}

const quantityDetails = new Set([
  'today_volume', 'yesterday_volume', 'previous_volume', 'frozen', 'frozen_today', 'frozen_yesterday', 'long_frozen', 'short_frozen',
  'available', 'available_today', 'available_now', 'combination_volume', 'comb_long_frozen', 'comb_short_frozen',
  'strike_frozen', 'abandon_frozen', 'open_volume', 'close_volume', 'tas_volume', 'last_trade_volume', 'credit_sellable_volume',
  'native_updated_at', 'risk_updated_at', 'adl', 'change_reason',
])

export function positionDetails(position: Position, channel?: string): { key: string; label: string; text: string }[] {
  const attrs = positionAttributes(position)
  const shown = new Set(positionColumns(channel).map(column => column.key))
  if (shown.has('today_yesterday')) { shown.add('today_volume'); shown.add('yesterday_volume') }
  if (channel === 'gm') shown.add('available_now')
  return Object.entries(detailLabels).flatMap(([key, label]) => {
    const value = attrs[key]
    if (shown.has(key) || value == null || value === '') return []
    const text = key === 'margin_asset' ? positionCurrencyUnit(value) : typeof value === 'string' ? value
      : quantityDetails.has(key) ? positionText(value) : positionDecimalText(value)
    return text === '—' ? [] : [{ key, label, text }]
  })
}

export function positionValueLabel(channel?: string): string {
  return channel === 'ctp' || channel === 'tq' ? '货值' : '市值'
}
