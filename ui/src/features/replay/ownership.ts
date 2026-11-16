import type { ReplayOrder } from '@/features/replay/types'

export type ReplayOwnershipKind = 'current' | 'other' | 'manual' | 'external' | 'unknown'
export interface ReplayOwnership { kind: ReplayOwnershipKind; label: string; executionId: string | null }

const labels: Record<ReplayOwnershipKind, string> = { current: '本次执行', other: '其他执行', manual: '人工', external: '外部', unknown: '归属未知' }
const order: ReplayOwnershipKind[] = ['current', 'other', 'manual', 'external', 'unknown']

/** 归属只采用已有证明；缺少执行身份不代表人工交易。 */
export function replayOwnership(value: { execution_id?: unknown; origin?: unknown; field_quality?: unknown }, executionId: string): ReplayOwnership {
  const quality = value.field_quality != null && typeof value.field_quality === 'object' && !Array.isArray(value.field_quality)
    ? value.field_quality as Record<string, unknown> : {}
  const known = (field: 'execution_id' | 'origin') => quality[field] !== 'missing' && quality[field] !== 'invalid'
    && typeof value[field] === 'string' && value[field] !== '' ? value[field] as string : null
  const owner = known('execution_id'), origin = known('origin')
  const kind = origin === 'manual' || origin === 'external' ? owner ? 'unknown' : origin
    : owner ? owner === executionId ? 'current' : 'other' : 'unknown'
  return { kind, label: labels[kind], executionId: owner }
}

export function replayOrderOwnershipGroups(orders: ReplayOrder[], executionId: string) {
  const groups = new Map<ReplayOwnershipKind, { kind: ReplayOwnershipKind; label: string; orders: ReplayOrder[] }>()
  for (const value of orders) {
    const { kind, label } = replayOwnership(value, executionId)
    const group = groups.get(kind) ?? { kind, label, orders: [] }
    group.orders.push(value); groups.set(kind, group)
  }
  return order.flatMap(kind => { const group = groups.get(kind); return group ? [group] : [] })
}
