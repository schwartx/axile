import type {
  AccountControlOperationOverride,
  AccountControlOverride,
  AccountControlPolicy,
  AccountControlRule,
  AccountControlRuleOverride,
  AccountControlScope,
  AccountControlScopeOverride,
} from '@/types/api'

type RuleKey = 'per_minute' | 'per_day' | 'min_interval_ms'

const RULE_KEYS: RuleKey[] = ['per_minute', 'per_day', 'min_interval_ms']

function normalizedRule(value: AccountControlRuleOverride | null | undefined): AccountControlRuleOverride | null {
  if (!value) return null
  // 显式解除：与 limit/on_trigger 互斥，归一化只保留 unlimited 标记
  if (value.unlimited) return { unlimited: true }
  if (value.limit == null && value.on_trigger == null) return null
  return {
    ...(value.limit != null ? { limit: value.limit } : {}),
    ...(value.on_trigger != null ? { on_trigger: value.on_trigger } : {}),
  }
}

function normalizedScope(value: AccountControlScopeOverride | null | undefined): AccountControlScopeOverride | null {
  if (!value) return null
  const entries = RULE_KEYS.flatMap((key) => {
    const rule = normalizedRule(value[key])
    return rule ? [[key, rule] as const] : []
  })
  return entries.length ? Object.fromEntries(entries) : null
}

export function normalizedOperationOverride(
  value: AccountControlOperationOverride | null | undefined,
): AccountControlOperationOverride | null {
  if (!value) return null
  const account = normalizedScope(value.account)
  const symbol = normalizedScope(value.symbol)
  if (value.priority == null && value.daily_warning_percent == null && !account && !symbol) return null
  return {
    ...(value.priority != null ? { priority: value.priority } : {}),
    ...(value.daily_warning_percent != null ? { daily_warning_percent: value.daily_warning_percent } : {}),
    ...(account ? { account } : {}),
    ...(symbol ? { symbol } : {}),
  }
}

export function normalizedAccountControlOverride(value: AccountControlOverride | null): AccountControlOverride | null {
  if (!value) return null
  const operations = Object.fromEntries(
    Object.entries(value.operations ?? {}).flatMap(([key, operation]) => {
      const normalized = normalizedOperationOverride(operation)
      return normalized ? [[key, normalized]] : []
    }),
  )
  const groups = Object.fromEntries(
    Object.entries(value.groups ?? {}).flatMap(([key, scope]) => {
      const normalized = normalizedScope(scope)
      return normalized ? [[key, normalized]] : []
    }),
  )
  if (!Object.keys(operations).length && !Object.keys(groups).length && !value.timezone) return null
  return { ...(value.timezone ? { timezone: value.timezone } : {}), operations, groups }
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonical(item)]),
    )
  }
  return value
}

export function sameAccountControlOverride(
  left: AccountControlOverride | null,
  right: AccountControlOverride | null,
): boolean {
  return JSON.stringify(canonical(normalizedAccountControlOverride(left)))
    === JSON.stringify(canonical(normalizedAccountControlOverride(right)))
}

function countScopeOverrides(value: AccountControlScopeOverride | null | undefined): number {
  return RULE_KEYS.filter((key) => normalizedRule(value?.[key])).length
}

export function countOperationOverrides(value: AccountControlOperationOverride | null | undefined): number {
  if (!value) return 0
  return (value.priority != null ? 1 : 0)
    + (value.daily_warning_percent != null ? 1 : 0)
    + countScopeOverrides(value.account)
    + countScopeOverrides(value.symbol)
}

export function countAccountControlOverrides(value: AccountControlOverride | null): number {
  if (!value) return 0
  const operationCount = Object.values(value.operations ?? {}).reduce(
    (sum, operation) => sum + countOperationOverrides(operation),
    0,
  )
  const groupCount = Object.values(value.groups ?? {}).reduce(
    (sum, scope) => sum + countScopeOverrides(scope),
    0,
  )
  return operationCount + groupCount
}

export function resolveAccountControlRule(
  base: AccountControlRule | null | undefined,
  override: AccountControlRuleOverride | null | undefined,
): AccountControlRule | null {
  if (!override) return base ?? null
  // 显式解除：无论基线为何都解析为无限制（与后端 _merge_rule 对齐）
  if (override.unlimited) return null
  if (!base && override.limit == null) return null
  return {
    limit: override.limit ?? base?.limit ?? 1,
    on_trigger: override.on_trigger ?? base?.on_trigger ?? 'wait',
  }
}

export function resolveAccountControlScope(
  base: AccountControlScope | null | undefined,
  override: AccountControlScopeOverride | null | undefined,
): AccountControlScope {
  return {
    per_minute: resolveAccountControlRule(base?.per_minute, override?.per_minute),
    per_day: resolveAccountControlRule(base?.per_day, override?.per_day),
    min_interval_ms: resolveAccountControlRule(base?.min_interval_ms, override?.min_interval_ms),
  }
}

export function resolveAccountControlPolicy(
  base: AccountControlPolicy,
  override: AccountControlOverride,
): AccountControlPolicy {
  const operations = { ...base.operations }
  for (const [key, operationOverride] of Object.entries(override.operations ?? {})) {
    const current = operations[key] ?? { priority: 100, account: {} }
    operations[key] = {
      priority: operationOverride.priority ?? current.priority,
      daily_warning_percent: operationOverride.daily_warning_percent ?? null,
      account: resolveAccountControlScope(current.account, operationOverride.account),
      symbol: current.symbol || operationOverride.symbol
        ? resolveAccountControlScope(current.symbol, operationOverride.symbol)
        : null,
    }
  }
  const groups = { ...base.groups }
  for (const [key, groupOverride] of Object.entries(override.groups ?? {})) {
    groups[key] = resolveAccountControlScope(groups[key], groupOverride)
  }
  return { timezone: override.timezone ?? base.timezone, operations, groups }
}
