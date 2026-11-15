import { algorithmRefOf, describeAlgorithmRef, validateAlgorithmRef } from '@/features/setup/algorithms'
import type { Account, ProductAlgorithm } from '@/types/api'

/** 期货界面只显示品种，证券代码保留交易所；内部配置键保持不变。 */
export function algorithmScopeLabel(channel: string, code: string): string {
  return channel === 'ctp' || channel === 'tq' ? code.replace(/^[A-Za-z]+\./, '') : code
}

export function productAlgorithmDraft(values: Record<string, ProductAlgorithm> = {}): Record<string, ProductAlgorithm> {
  return structuredClone(values)
}

export function productAlgorithmValue(values: Record<string, ProductAlgorithm>): Record<string, ProductAlgorithm> {
  return Object.fromEntries(Object.entries(values).sort(([a], [b]) => a.localeCompare(b)))
}

export function productAlgorithmError(values: Record<string, ProductAlgorithm>): string | null {
  for (const [key, profile] of Object.entries(values)) {
    const error = validateAlgorithmRef(profile.algorithm)
    if (error) return `${key}：${error}`
  }
  return null
}

/** 沿用 next 的品种计数摘要，不引入时段配置。 */
export function productAlgorithmSummary(account: Pick<Account, 'algorithm' | 'product_algorithms'>): string {
  const count = Object.keys(account.product_algorithms ?? {}).length
  return `${describeAlgorithmRef(algorithmRefOf(account.algorithm))}${count ? ` · ${count} 个品种` : ''}`
}
