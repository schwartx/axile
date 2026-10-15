/** 与服务端使用同一取整规则；小额度可能到用尽时才达到预警线。 */
export function quotaWarningCount(limit: number, percent: number): number {
  return Math.ceil(limit * percent / 100)
}

export function quotaWarningPercentError(value: number | null | undefined): string | null {
  if (value == null) return null
  return Number.isInteger(value) && value >= 1 && value <= 99 ? null : '请输入 1–99 的整数'
}
