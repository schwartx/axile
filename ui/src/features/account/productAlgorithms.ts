import { algorithmScheduleDraft, algorithmScheduleErrors, algorithmScheduleValue, type AlgorithmRuleDraft } from '@/features/account/algorithmSchedule'
import { validateAlgorithmRef } from '@/features/setup/algorithms'
import type { ProductAlgorithm } from '@/types/api'

export type ProductAlgorithmDraft = { algorithm: ProductAlgorithm['algorithm']; schedule: AlgorithmRuleDraft[] }

export function productAlgorithmDraft(values: Record<string, ProductAlgorithm> = {}, previous: Record<string, ProductAlgorithmDraft> = {}): Record<string, ProductAlgorithmDraft> {
  return Object.fromEntries(Object.entries(values).map(([key, profile]) => [key, {
    algorithm: structuredClone(profile.algorithm), schedule: algorithmScheduleDraft(profile.algorithm_schedule ?? [], previous[key]?.schedule),
  }]))
}

export function productAlgorithmValue(values: Record<string, ProductAlgorithmDraft>): Record<string, ProductAlgorithm> {
  return Object.fromEntries(Object.entries(values).sort(([a], [b]) => a.localeCompare(b)).map(([key, profile]) => [key, {
    algorithm: profile.algorithm, algorithm_schedule: algorithmScheduleValue(profile.schedule),
  }]))
}

export function productAlgorithmError(values: Record<string, ProductAlgorithmDraft>): string | null {
  for (const [key, profile] of Object.entries(values)) {
    const error = validateAlgorithmRef(profile.algorithm) ?? algorithmScheduleErrors(profile.schedule).find(Boolean)
    if (error) return `${key}：${error}`
  }
  return null
}
