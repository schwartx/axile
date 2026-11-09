import type { TradeChannel } from '@/types/api'
import { getChannelDescriptor } from '@/stores/channels'

/** 返回渠道对应的账户执行超时默认值（秒）。 */
export function defaultExecutionTimeoutForChannel(channel: TradeChannel): string {
  return String(getChannelDescriptor(channel)?.defaults.execution_timeout ?? 300)
}

/** 校验账户执行超时，与服务端一致，仅要求正整数秒。 */
export function executionTimeoutError(value: string, options: { allowEmpty?: boolean } = {}): string | null {
  if (value.trim() === '') return options.allowEmpty ? null : '请输入整数秒'
  const timeout = Number(value)
  if (!Number.isInteger(timeout)) return '需为整数秒'
  if (timeout < 1) return '需 ≥ 1 秒（该兜底不可关闭）'
  return null
}
