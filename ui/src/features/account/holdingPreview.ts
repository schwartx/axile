import type { RebalancePlan } from '@/lib/derive'
import type { AccountRebalancePlan, Position } from '@/types/api'

/** 到位结论读取完整计划，不能把缺少权重的数量行当成空仓。 */
export function rebalanceHeadline(plan: AccountRebalancePlan | null, holdingsCount: number | null): string {
  if (plan?.off_symbol_count == null) return '偏离尚未计算'
  if (plan.off_symbol_count > 0) {
    return holdingsCount === 0 && plan.rows.length > 0 && plan.rows.every(row => row.action === 'open')
      ? `空仓 · ${plan.off_symbol_count} 只待建仓`
      : `${plan.off_symbol_count} 只待调整`
  }
  return holdingsCount === 0 && plan.rows.length === 0 ? '空仓 · 与目标一致' : '已调仓到位'
}

export interface CurrentHoldingPreview {
  key: string
  symbol: string
  direction: 'long' | 'short'
  weight: number | null
  volume: number | null
  availableVolume: number | null
  value: number | null
}

interface HoldingAggregate {
  symbol: string
  direction: 'long' | 'short'
  volume: number
  volumeComplete: boolean
  availableVolume: number
  availableVolumeComplete: boolean
  value: number | null
  valueComplete: boolean
}

function isShort(direction: unknown): boolean {
  return typeof direction === 'string' && (direction.includes('空') || direction.toLowerCase().includes('short'))
}

function quantity(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

/** 紧凑显示跨渠道持仓数量，保留最多六位有效小数。 */
export function formatHoldingQuantity(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  if (value === 0) return '0'
  if (value < 0.000001) return '<0.000001'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 6 })
}

/** 按品种与方向聚合当前持仓，并按持仓市值绝对值降序排列。 */
export function currentHoldingPreview(
  positions: Position[],
  equity: number,
): CurrentHoldingPreview[] {
  const holdings = new Map<string, HoldingAggregate>()

  for (const position of positions) {
    const symbol = typeof position.symbol === 'string' ? position.symbol.trim() : ''
    if (!symbol) continue
    const direction = isShort(position.direction) ? 'short' : 'long'
    const key = `${symbol}:${direction}`
    const rawValue = position.market_value
    const value = typeof rawValue === 'number' && Number.isFinite(rawValue) ? Math.abs(rawValue) : null
    const volume = quantity(position.volume)
    const availableVolume = quantity(position.available_volume)
    const previous = holdings.get(key)
    holdings.set(key, {
      symbol,
      direction,
      volume: (previous?.volume ?? 0) + (volume ?? 0),
      volumeComplete: (previous?.volumeComplete ?? true) && volume != null,
      availableVolume: (previous?.availableVolume ?? 0) + (availableVolume ?? 0),
      availableVolumeComplete: (previous?.availableVolumeComplete ?? true) && availableVolume != null,
      value: value == null ? previous?.value ?? null : (previous?.value ?? 0) + value,
      valueComplete: (previous?.valueComplete ?? true) && value != null,
    })
  }

  return [...holdings.entries()]
    .map(([key, holding]) => {
      const signedValue = !holding.valueComplete || holding.value == null
        ? null
        : holding.direction === 'short' ? -holding.value : holding.value
      return {
        key,
        symbol: holding.symbol,
        direction: holding.direction,
        weight: signedValue != null && equity > 0 ? (signedValue / equity) * 100 : null,
        volume: holding.volumeComplete ? holding.volume : null,
        availableVolume: holding.availableVolumeComplete ? holding.availableVolume : null,
        value: signedValue,
      }
    })
    .toSorted((left, right) => Math.abs(right.value ?? 0) - Math.abs(left.value ?? 0))
}

/** 预计总换手为所有待调整品种绝对权重差之和。 */
export function rebalanceTurnover(plan: RebalancePlan): number {
  return plan.rows
    .filter((row) => row.action !== 'aligned')
    .reduce((sum, row) => sum + row.amount, 0)
}
