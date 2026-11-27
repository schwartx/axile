import { connectionValueError, normalizeMoneyValue } from '@/components/ui/connectionFieldValue'
import type { ChannelAccountFieldConstraints, ChannelCapability, Position } from '@/types/api'

export const SIGNAL_CHANNEL = 'rebalance_signal'
export const TUSHARE_API_URL = 'https://api.tushare.pro'
export const GM_ADDRESS_LABEL = '终端 RPC 地址'
export const TUSHARE_URL_CONSTRAINTS: ChannelAccountFieldConstraints = {
  endpoint: { scheme: 'required', allowed_schemes: ['http', 'https'], port: 'optional', allow_path: true },
}
export const GM_ADDRESS_CONSTRAINTS: ChannelAccountFieldConstraints = {
  endpoint: { scheme: 'forbidden', allowed_schemes: [], port: 'required', allow_path: false },
}
const CTP_FRONT_CONSTRAINTS: ChannelAccountFieldConstraints = {
  endpoint: { scheme: 'required', allowed_schemes: ['tcp'], port: 'required', allow_path: false },
}

export type SignalQuoteSource =
  | { kind: 'gm'; token: string; connection_mode: 'terminal' | 'service'; serv_addr: string | null; terminal_path?: string }
  | { kind: 'tushare'; token: string; url: string }
  | { kind: 'ctp'; md_front: string }
  | { kind: 'tq'; tq_username: string; tq_password: string }
  | { kind: 'custom'; code: string }

export function signalQuoteOptions(market: string, channels: ChannelCapability[]) {
  const options = [
    { market: 'ashare', value: 'gm', label: '掘金' },
    { market: 'ashare', value: 'tushare', label: 'Tushare' },
    { market: 'ctp', value: 'ctp', label: 'CTP' },
    { market: 'ctp', value: 'tq', label: '天勤' },
    { market: null, value: 'custom', label: '自定义函数' },
  ] as const
  return options.filter((option) => option.market === null || (
    option.market === market && (
      option.value === 'tushare' || channels.some((channel) => channel.channel === option.value && channel.independent_market_data)
    )
  ))
}

export interface SignalAssetSnapshot {
  total_asset: number
  available_cash: number
  currency: string
  positions: Position[] | null
}

export interface SignalConfig {
  manual_assets: SignalAssetSnapshot | null
  account_code: string | null
  follow_target?: boolean
  follow_fee_bps?: number
  quote_source: SignalQuoteSource
}

export interface PositionDraft {
  id: string
  symbol: string
  direction: '多头' | '空头'
  volume: string
  available: string
  availableFromVolume?: boolean
}

export function positionVolumePatch(row: PositionDraft, volume: string): Partial<PositionDraft> {
  return !row.available || row.availableFromVolume
    ? { volume, available: volume, availableFromVolume: true }
    : { volume }
}

export interface SignalDraft {
  mode: 'manual' | 'function'
  equity: string
  cash: string
  cashFromEquity?: boolean
  currency: string
  positionsKnown: boolean
  followTarget: boolean
  followFeeBps: string
  positions: PositionDraft[]
  accountCode: string
  quoteKind: SignalQuoteSource['kind'] | ''
  gmToken: string
  gmMode: 'terminal' | 'service'
  gmTerminalPath: string | null
  gmAddress: string
  tushareToken: string
  tushareUrl: string
  md_front: string
  tq_username: string
  tq_password: string
  quoteCode: string
}

export function signalEquityPatch(draft: SignalDraft, equity: string): Partial<SignalDraft> {
  return !draft.cash || draft.cashFromEquity
    ? { equity, cash: equity, cashFromEquity: true }
    : { equity }
}

export const initialSignalDraft: SignalDraft = {
  mode: 'manual', equity: '', cash: '', currency: 'CNY', positionsKnown: false, followTarget: true, followFeeBps: '5', positions: [],
  accountCode: `from axile.channels.rebalance_signal import SignalAccountContext

def fetch_account(_context: SignalAccountContext) -> dict[str, object]:
    # 试跑用的示例快照。创建账户前请接入你的账户数据。
    return {
        "total_asset": 100000,
        "available_cash": 80000,
        "currency": "CNY",
        "positions": [
            {
                "symbol": "600000.SH",
                "direction": "多头",
                "volume": 1000,
                "available_volume": 1000,
                "market_value": None,
            },
            {
                "symbol": "510300.SH",
                "direction": "多头",
                "volume": 2000,
                "available_volume": 1500,
                "market_value": None,
            },
        ],
    }
`,
  quoteKind: 'gm', gmToken: '', gmMode: 'service', gmTerminalPath: '', gmAddress: '', tushareToken: '', tushareUrl: TUSHARE_API_URL,
  md_front: '', tq_username: '', tq_password: '',
  quoteCode: `from axile.executor.algorithms.utils.clock import clock_now
from axile.executor.models.unified_price import UnifiedPriceData

def fetch_quotes(symbols: list[str]) -> dict[str, UnifiedPriceData]:
    # 测试用的演示行情。接入实际行情时，替换下方的价格和盘口数据。
    now = clock_now()
    return {
        symbol: UnifiedPriceData(
            symbol=symbol,
            last_price=10.0,
            bids=[
                (9.99, 100.0),
                (9.98, 180.0),
                (9.97, 250.0),
                (9.96, 320.0),
                (9.95, 460.0),
            ],
            asks=[
                (10.01, 100.0),
                (10.02, 150.0),
                (10.03, 230.0),
                (10.04, 310.0),
                (10.05, 420.0),
            ],
            volume=1000.0,
            timestamp=int(now.timestamp() * 1000),
            update_time=now.isoformat(),
        )
        for symbol in symbols
    }
`,
}

const previousAccountExample = `def fetch_account(context):
    # 返回权益、可用资金、币种和可选持仓。
    raise NotImplementedError("请提供账户数据源")`

export function refreshSignalAccountExample(code: string): string {
  return code.trim() === previousAccountExample ? initialSignalDraft.accountCode : code
}

export function isSignalAccountExample(code: string): boolean {
  return refreshSignalAccountExample(code).trim() === initialSignalDraft.accountCode.trim()
}

export function configToSignalDraft(config: SignalConfig): SignalDraft {
  const manual = config.manual_assets
  const quotes = config.quote_source
  return {
    ...initialSignalDraft,
    mode: config.account_code == null ? 'manual' : 'function',
    followTarget: config.follow_target ?? false,
    followFeeBps: config.follow_fee_bps == null ? initialSignalDraft.followFeeBps : String(config.follow_fee_bps),
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
    quoteKind: quotes.kind,
    ...(quotes.kind === 'gm' ? {
      gmToken: quotes.token, gmMode: quotes.connection_mode,
      gmTerminalPath: quotes.terminal_path ?? (quotes.connection_mode === 'terminal' ? null : ''),
      gmAddress: quotes.serv_addr ?? '',
    } : {}),
    ...(quotes.kind === 'tushare' ? { tushareToken: quotes.token, tushareUrl: quotes.url } : {}),
    ...(quotes.kind === 'ctp' ? { md_front: quotes.md_front } : {}),
    ...(quotes.kind === 'tq' ? { tq_username: quotes.tq_username, tq_password: quotes.tq_password } : {}),
    ...(quotes.kind === 'custom' ? { quoteCode: quotes.code } : {}),
  }
}

function amount(value: string): number {
  return value.trim() ? Number(normalizeMoneyValue(value) ?? value) : NaN
}

type SignalQuoteField = 'quoteKind' | 'gmToken' | 'gmAddress' | 'gmTerminalPath' | 'tushareToken' | 'tushareUrl' | 'quoteCode'
  | 'md_front' | 'tq_username' | 'tq_password'
export type SignalQuoteErrors = Partial<Record<SignalQuoteField, string | null>>

export function signalQuoteErrors(draft: SignalDraft): SignalQuoteErrors {
  switch (draft.quoteKind) {
    case 'gm':
      return {
        gmToken: connectionValueError({ kind: 'secret', value: draft.gmToken, required: true, label: '掘金 Token' }),
        ...(draft.gmMode === 'service' ? {
          gmAddress: connectionValueError({ kind: 'endpoint', value: draft.gmAddress, required: true, label: GM_ADDRESS_LABEL, constraints: GM_ADDRESS_CONSTRAINTS }),
        } : draft.gmTerminalPath === null ? {} : {
          gmTerminalPath: connectionValueError({ kind: 'directory', value: draft.gmTerminalPath, required: true, label: '本机终端目录' }),
        }),
      }
    case 'tushare':
      return {
        tushareToken: connectionValueError({ kind: 'secret', value: draft.tushareToken, required: true, label: 'Tushare Token' }),
        tushareUrl: connectionValueError({ kind: 'endpoint', value: draft.tushareUrl.trim() || TUSHARE_API_URL, required: true, label: 'API URL', constraints: TUSHARE_URL_CONSTRAINTS }),
      }
    case 'ctp':
      return {
        md_front: connectionValueError({ kind: 'endpoint', value: draft.md_front, required: true, label: '行情前置', constraints: CTP_FRONT_CONSTRAINTS }),
      }
    case 'tq':
      return {
        tq_username: connectionValueError({ kind: 'identifier', value: draft.tq_username, required: true, label: '天勤账号' }),
        tq_password: connectionValueError({ kind: 'secret', value: draft.tq_password, required: true, label: '天勤密码' }),
      }
    case 'custom':
      return draft.quoteCode.trim() ? {} : { quoteCode: '请填写行情函数' }
    default:
      return { quoteKind: '请选择行情来源' }
  }
}

export function signalQuoteConfig(draft: SignalDraft): SignalQuoteSource {
  const error = Object.values(signalQuoteErrors(draft)).find(Boolean)
  if (error) throw new Error(error)
  switch (draft.quoteKind) {
    case 'gm': {
      const address = draft.gmMode === 'service' ? draft.gmAddress.trim() : null
      const terminal = draft.gmMode === 'terminal' ? draft.gmTerminalPath?.trim() : undefined
      return { kind: 'gm', token: draft.gmToken.trim(), connection_mode: draft.gmMode, serv_addr: address, ...(terminal ? { terminal_path: terminal } : {}) }
    }
    case 'tushare':
      return { kind: 'tushare', token: draft.tushareToken.trim(), url: draft.tushareUrl.trim() || TUSHARE_API_URL }
    case 'ctp':
      return { kind: 'ctp', md_front: draft.md_front.trim() }
    case 'tq':
      return { kind: 'tq', tq_username: draft.tq_username, tq_password: draft.tq_password }
    case 'custom':
      return { kind: 'custom', code: draft.quoteCode }
    default:
      throw new Error('请选择行情来源')
  }
}

export type SignalPositionErrors = Partial<Record<'symbol' | 'volume' | 'available', string>>
export interface SignalAccountErrors {
  accountCode?: string
  equity?: string
  followFeeBps?: string
  cash?: string
  currency?: string
  positions?: Record<string, SignalPositionErrors>
}

export function signalAccountErrors(draft: SignalDraft, market?: string): SignalAccountErrors {
  if (draft.mode === 'function') {
    if (!draft.accountCode.trim()) return { accountCode: '请填写账户函数' }
    return isSignalAccountExample(draft.accountCode) ? { accountCode: '示例快照用于测试，请接入你的账户数据后继续' } : {}
  }
  const errors: SignalAccountErrors = {}
  const followLedger = market === 'A股' && draft.followTarget
  if (!followLedger && !Number.isFinite(amount(draft.equity))) errors.equity = '请填写有效的账户权益'
  if (!Number.isFinite(amount(draft.cash))) errors.cash = '请填写有效的可用资金'
  if (!draft.currency.trim()) errors.currency = '请填写计价币种'
  const fee = amount(draft.followFeeBps)
  if (followLedger && !(fee >= 0 && fee <= 10_000)) errors.followFeeBps = '请输入 0 到 10000 之间的基点数'
  if (!draft.positionsKnown) return errors

  const positions: Record<string, SignalPositionErrors> = {}
  const seen = new Set<string>()
  for (const row of draft.positions) {
    const symbol = row.symbol.trim()
    const volume = amount(row.volume), available = amount(row.available)
    const key = JSON.stringify([symbol, row.direction])
    const fields: SignalPositionErrors = {}
    if (!symbol) fields.symbol = '请填写标的'
    else if (seen.has(key)) fields.symbol = '该标的和方向已录入'
    else seen.add(key)
    if (!Number.isFinite(volume) || volume < 0) fields.volume = '请填写非负数量'
    if (!Number.isFinite(available) || available < 0) fields.available = '请填写非负数量'
    else if (!fields.volume && available > volume) fields.available = '可用数量不能超过持仓数量'
    if (Object.keys(fields).length) positions[row.id] = fields
  }
  if (Object.keys(positions).length) errors.positions = positions
  return errors
}

export function signalAccountConfig(draft: SignalDraft, market?: string): Pick<SignalConfig, 'manual_assets' | 'account_code' | 'follow_target' | 'follow_fee_bps'> {
  const errors = signalAccountErrors(draft, market)
  const firstPosition = Object.values(errors.positions ?? {})[0]
  const error = errors.accountCode ?? errors.equity ?? errors.cash ?? errors.currency ?? errors.followFeeBps ?? Object.values(firstPosition ?? {})[0]
  if (error) throw new Error(error)
  if (draft.mode === 'function') return { manual_assets: null, account_code: draft.accountCode, follow_target: false }
  const positions = draft.positionsKnown ? draft.positions.map((row) => ({
    symbol: row.symbol.trim(), direction: row.direction, volume: amount(row.volume),
    available_volume: amount(row.available), market_value: null,
  })) : null
  const cash = amount(draft.cash)
  const equity = amount(draft.equity)
  return {
    account_code: null,
    follow_target: draft.followTarget,
    ...(market === 'A股' && draft.followTarget ? { follow_fee_bps: amount(draft.followFeeBps) } : {}),
    manual_assets: { total_asset: Number.isFinite(equity) ? equity : cash, available_cash: cash, currency: draft.currency.trim(), positions },
  }
}

export function signalConfig(draft: SignalDraft, market?: string): SignalConfig {
  return { quote_source: signalQuoteConfig(draft), ...signalAccountConfig(draft, market) }
}
