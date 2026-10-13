import { channelAccountFieldVisible } from '@/features/setup/channelAccountFields'
import type { Account, ChannelAccountField } from '@/types/api'

/** 从已保存的连接值初始化编辑草稿。 */
export function initialConnectionDraft(
  account: Pick<Account, 'connection_values'>,
  fields: ChannelAccountField[],
): Record<string, unknown> {
  return Object.fromEntries(fields.map((field) => [
    field.name,
    account.connection_values?.[field.name] ?? field.default ?? '',
  ]))
}

/** 将可见连接字段转换为更新载荷。 */
export function mergedConnectionConfig(
  fields: ChannelAccountField[],
  draft: Record<string, unknown>,
) {
  const next: Record<string, unknown> = {}
  for (const field of fields) {
    if (!channelAccountFieldVisible(field, draft)) continue
    const value = draft[field.name]
    if (value !== undefined && value !== null) next[field.name] = value
  }
  return next
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

/** 比较连接配置的语义内容，忽略 JSON 对象键顺序。 */
export function sameConnectionConfig(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right))
}
