import type { Position } from '@/types/api'

export const SIGNAL_CHANNEL = 'rebalance_signal'

export interface SignalConfig {
  manual_assets: {
    total_asset: number
    available_cash: number
    currency: string
    positions: Position[] | null
  } | null
  account_code: string | null
  quote_code: string
}

export interface PositionDraft {
  id: string
  symbol: string
  direction: '多头' | '空头'
  volume: string
  available: string
}

export interface SignalDraft {
  mode: 'manual' | 'function'
  equity: string
  cash: string
  currency: string
  positionsKnown: boolean
  positions: PositionDraft[]
  accountCode: string
  quoteCode: string
}

export const initialSignalDraft: SignalDraft = {
  mode: 'manual', equity: '', cash: '', currency: 'CNY', positionsKnown: false, positions: [],
  accountCode: 'def fetch_account(context):\n    # 返回权益、可用资金、币种和可选持仓。\n    raise NotImplementedError("请提供账户数据源")\n',
  quoteCode: 'def fetch_quotes(symbols):\n    # 返回 {标的: {"last_price": 价格, "update_time": 带时区的 ISO 时间}}。\n    raise NotImplementedError("请选择行情数据源")\n',
}

export function configToSignalDraft(config: SignalConfig): SignalDraft {
  const manual = config.manual_assets
  return {
    ...initialSignalDraft,
    mode: config.account_code == null ? 'manual' : 'function',
    equity: manual ? String(manual.total_asset) : '',
    cash: manual ? String(manual.available_cash) : '',
    currency: manual?.currency ?? 'CNY',
    positionsKnown: manual?.positions != null,
    positions: (manual?.positions ?? []).map((position) => ({
      id: crypto.randomUUID(),
      symbol: position.symbol ?? '',
      direction: position.direction === '空头' ? '空头' : '多头',
      volume: String(position.volume ?? 0),
      available: String(position.available_volume ?? 0),
    })),
    accountCode: config.account_code ?? initialSignalDraft.accountCode,
    quoteCode: config.quote_code,
  }
}

function amount(value: string): number {
  return value.trim() ? Number(value) : NaN
}

export function validateSignalQuoteSetup(quoteCode: string) {
  if (!quoteCode.trim() || quoteCode.trim() === initialSignalDraft.quoteCode.trim()) {
    throw new Error('请填写行情函数，用你的行情源替换默认模板')
  }
}

export function signalAccountConfig(draft: SignalDraft): Pick<SignalConfig, 'manual_assets' | 'account_code'> {
  if (draft.mode === 'function') {
    if (!draft.accountCode.trim()) throw new Error('请填写账户函数')
    return { manual_assets: null, account_code: draft.accountCode }
  }
  const total = amount(draft.equity), cash = amount(draft.cash)
  if (!Number.isFinite(total) || !Number.isFinite(cash) || !draft.currency.trim()) {
    throw new Error('请填写账户权益、可用资金和币种')
  }
  const positions = draft.positionsKnown ? draft.positions.map((row) => {
    const volume = amount(row.volume), available = amount(row.available)
    if (!row.symbol.trim() || !Number.isFinite(volume) || !Number.isFinite(available) || available < 0 || volume < available) {
      throw new Error('请为每条持仓填写标的、持仓数量和可用数量')
    }
    return {
      symbol: row.symbol.trim(), direction: row.direction, volume, available_volume: available, market_value: null,
    }
  }) : null
  return {
    account_code: null,
    manual_assets: { total_asset: total, available_cash: cash, currency: draft.currency.trim(), positions },
  }
}

export function signalConfig(draft: SignalDraft): SignalConfig {
  if (!draft.quoteCode.trim()) throw new Error('请填写行情函数')
  return { ...signalAccountConfig(draft), quote_code: draft.quoteCode }
}
