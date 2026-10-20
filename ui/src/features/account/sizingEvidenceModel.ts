import type { TargetSizingAvailability, TargetSizingRow } from '@/types/api'
import { displayCurrencyUnit, fmtMoney } from '@/lib/format'

const EPS = 1e-9
type SizingEvidenceRow = Partial<TargetSizingRow>
const finiteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

export function sizingNumber(value: unknown, maximumFractionDigits = 4): string {
  if (!finiteNumber(value)) return '—'
  return Math.abs(value).toLocaleString('zh-CN', { maximumFractionDigits })
}

export function weightText(value: number | null | undefined): string {
  if (!finiteNumber(value)) return '—'
  if (Math.abs(value) <= EPS) return '空仓'
  return `${value > 0 ? '多' : '空'}${sizingNumber(value * 100, 2)}%`
}

export function quantityText(
  value: number | null | undefined,
  quantityLabel = '',
): string {
  if (!finiteNumber(value)) return '—'
  const direction = Math.abs(value) > EPS ? Math.sign(value) : 0
  const prefix = direction < 0 ? '空' : direction > 0 ? '多' : ''
  return `${prefix}${sizingNumber(value, 6)}${quantityLabel}`
}

export function targetQuantityText(value: number | null | undefined, quantityLabel = ''): string {
  return !finiteNumber(value) ? '—' : `${sizingNumber(value, 6)}${quantityLabel}`
}

export function isQuantizedZero(row: SizingEvidenceRow | null | undefined): boolean {
  return Boolean(
    row
      && row.reason_code === 'COMMON.SIZING.BELOW_MIN_QUANTITY'
      && finiteNumber(row.raw_quantity)
      && Math.abs(row.raw_quantity) > EPS
      && finiteNumber(row.target_quantity)
      && Math.abs(row.target_quantity) <= EPS,
  )
}

export function sizingReasonText(row: SizingEvidenceRow | null | undefined, quantityLabel = ''): string {
  if (!row) return '—'
  if (isQuantizedZero(row)) return quantityLabel === '手' ? '不足1手' : '不足最小单位'
  const reason = typeof row.reason_code === 'string' ? row.reason_code : ''
  if (reason.endsWith('.SIZING.BELOW_MIN_NOTIONAL')) return '不足最小名义金额'
  if (reason.endsWith('.SIZING.PURE_REDUCE_BELOW_MIN_NOTIONAL')) return '纯减仓例外'
  if (row.reason_code === 'COMMON.SIZING.MISSING_MARKET_DATA') return '缺少当时行情'
  if (row.reason_code === 'COMMON.SIZING.INVALID_PRICE') return '当时价格无效'
  if (row.reason_code === 'COMMON.SIZING.MISSING_UNIT_MULTIPLIER') return '缺少合约乘数'
  if (row.reason_code === 'COMMON.SIZING.QUANTIZED') {
    return finiteNumber(row.quantity_step)
      ? `按${sizingNumber(row.quantity_step)}${quantityLabel || '单位'}取整`
      : '按交易单位取整'
  }
  if (row.reason_code === 'COMMON.SIZING.ZERO_TARGET') return '目标为空'
  if (row.reason_code === 'COMMON.SIZING.EXACT') return '精确换算'
  return reason.trim() ? reason : row.status === 'UNAVAILABLE' ? '换算不可用' : '—'
}

export function sizingAvailabilityText(status: TargetSizingAvailability): string {
  if (status === 'pending_execution') return '可执行数量将在下次执行时按当时行情生成'
  if (status === 'legacy') return '当时未记录换算依据'
  if (status === 'unavailable') return '当次换算依据不完整'
  return ''
}

export function executableTargetFallbackText(
  status: TargetSizingAvailability | null | undefined,
  row?: SizingEvidenceRow | null,
  quantityLabel = '',
): string {
  if (status === 'legacy') return '当时未记'
  if (status === 'unavailable') return row ? sizingReasonText(row, quantityLabel) : '无法换算'
  if (status === 'available') return '无法换算'
  return '待换算'
}

function currencySuffix(currency: unknown): string {
  const label = displayCurrencyUnit(typeof currency === 'string' ? currency : '')
  return label ? ` ${label}` : ''
}

function accountWeightFormula(row: SizingEvidenceRow): string | null {
  if (!finiteNumber(row.strategy_weight) || !finiteNumber(row.account_weight)) return null
  if (!finiteNumber(row.account_multiplier)) return `策略 ${weightText(row.strategy_weight)} → 账户 ${weightText(row.account_weight)}`
  return `策略 ${weightText(row.strategy_weight)} × 账户乘数 ${sizingNumber(row.account_multiplier, 4)} = 账户 ${weightText(row.account_weight)}`
}

function targetNotionalFormula(row: SizingEvidenceRow, currency: string): string | null {
  if (!finiteNumber(row.equity) || !finiteNumber(row.account_weight) || !finiteNumber(row.target_notional)) return null
  return `权益 ${fmtMoney(row.equity)} × ${sizingNumber(Math.abs(row.account_weight) * 100, 2)}% = 目标名义 ${fmtMoney(row.target_notional)}${currencySuffix(currency)}`
}

function unitNotionalFormula(row: SizingEvidenceRow, unit: string, currency: string): string | null {
  if (!finiteNumber(row.reference_price) || !finiteNumber(row.unit_notional)) return null
  const conversion = row.value_conversion
  const parts = [`价格 ${sizingNumber(row.reference_price, 6)}${currencySuffix(conversion?.price_currency ?? currency)}`]
  if (finiteNumber(row.unit_multiplier) && Math.abs(row.unit_multiplier - 1) > EPS) parts.push(`合约乘数 ${sizingNumber(row.unit_multiplier)}`)
  if (conversion && (conversion.price_currency !== conversion.value_currency || conversion.rate !== 1)) parts.push(`汇率 ${sizingNumber(conversion.rate, 8)}`)
  return `${parts.join(' × ')} = 每${unit || '单位'}名义 ${fmtMoney(row.unit_notional)}${currencySuffix(currency)}`
}

function rawQuantityFormula(row: SizingEvidenceRow, unit: string): string | null {
  if (!finiteNumber(row.raw_quantity) || !finiteNumber(row.target_notional) || !finiteNumber(row.unit_notional) || row.unit_notional <= 0) return null
  return `目标名义 ${fmtMoney(row.target_notional)} ÷ 每${unit || '单位'}名义 ${fmtMoney(row.unit_notional)} = ${sizingNumber(row.raw_quantity, 6)}${unit}`
}

/** 只解释记录中的每一步结果；数量目标不套用权重公式，跨币种单位价值保留汇率。 */
export function sizingFormulas(row: SizingEvidenceRow | null | undefined, quantityLabel: string, currency: string): string[] {
  if (!row) return []
  if (row.sizing_mode === 'quantity') {
    return [
      `直接数量目标 ${quantityText(row.raw_quantity ?? row.account_weight, quantityLabel)}`,
      `${sizingReasonText(row, quantityLabel)} → 可执行目标 ${quantityText(row.target_quantity, quantityLabel)}`,
    ]
  }
  const rows = [accountWeightFormula(row), targetNotionalFormula(row, currency),
    unitNotionalFormula(row, quantityLabel, currency), rawQuantityFormula(row, quantityLabel)]
    .filter((value): value is string => value != null)
  if (finiteNumber(row.raw_quantity)) rows.push(`未取整目标 ${quantityText(row.raw_quantity, quantityLabel)}`)
  if (finiteNumber(row.target_quantity)) rows.push(`${sizingReasonText(row, quantityLabel)} → 可执行目标 ${quantityText(row.target_quantity, quantityLabel)}`)
  return rows
}
