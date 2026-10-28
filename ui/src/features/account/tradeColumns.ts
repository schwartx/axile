import { displayCurrencyUnit } from '@/lib/format'
import { orderOffset } from '@/features/account/currentOrdersModel'
import type { AccountTrade } from '@/lib/api/orders'

export interface TradeColumn {
  key: string
  label: string
  truncate?: boolean
  numeric?: boolean
  text: (trade: AccountTrade) => string
  color?: (trade: AccountTrade) => string
}

const number = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—'
  : value !== 0 && Math.abs(value) < 1e-12 ? String(value) : value.toLocaleString('zh-CN', { maximumFractionDigits: 12 })
const money = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—'
  : value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const offset: TradeColumn = { key: 'offset', label: '开平', text: trade => trade.offset ? orderOffset(trade.offset) : '—' }
const fee: TradeColumn = { key: 'commission', label: '手续费', numeric: true, text: trade => number(trade.commission) }

const columns: Record<string, TradeColumn[]> = {
  ctp: [
    offset,
    { key: 'trading_day', label: '交易日', text: trade => trade.trading_day?.replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3') || '—' },
    { key: 'hedge_flag', label: '投机 / 套保', text: trade => ({ '1': '投机', '2': '套利', '3': '套保', '5': '做市', '6': '投机/套保', '7': '套保/投机' }[trade.hedge_flag ?? ''] ?? '—') },
  ],
  tq: [offset],
  gm: [
    { key: 'amount', label: '成交金额', numeric: true, text: trade => money(trade.amount) },
    { ...fee, text: trade => money(trade.commission) },
    { key: 'cost', label: '成本', numeric: true, text: trade => money(trade.cost) },
  ],
  external: [
    fee,
    { key: 'commission_asset', label: '手续费币种', text: trade => displayCurrencyUnit(trade.commission_asset) || '—' },
    { key: 'realized_pnl', label: '已实现盈亏', numeric: true, text: trade => number(trade.realized_pnl),
      color: trade => trade.realized_pnl == null || trade.realized_pnl === 0 ? '' : trade.realized_pnl > 0 ? 'text-up' : 'text-down' },
    { key: 'is_maker', label: 'Maker/Taker', text: trade => trade.is_maker == null ? '—' : trade.is_maker ? 'Maker' : 'Taker' },
    { key: 'position_side', label: '持仓方向', text: trade => ({ LONG: '多头', SHORT: '空头', BOTH: '单向' }[trade.position_side ?? ''] ?? '—') },
  ],
}

/** 渠道决定列，不因某一行缺失字段增删表头。 */
export function tradeColumns(channel: string | undefined): TradeColumn[] {
  return channel ? columns[channel] ?? columns.external : []
}
