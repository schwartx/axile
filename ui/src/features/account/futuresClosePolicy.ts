import type { FuturesClosePolicy, ClosePriority } from '@/types/api'

export const defaultClosePolicy = (): FuturesClosePolicy => ({ mode: 'rules', fallback: 'yesterday_first', overrides: {} })
export const closePriorityLabel = (value: ClosePriority): string => value === 'today_first' ? '先今后昨' : '先昨后今'
export function closePolicySummary(value: FuturesClosePolicy, compact = false): string {
  const count = Object.keys(value.overrides).length
  if (compact) {
    const mode = value.mode === 'fee' ? '费用优先' : count ? `${count} 项覆盖` : '品种预设'
    return `${mode} · ${closePriorityLabel(value.fallback)}`
  }
  const rules = count ? `${count} 个品种已修改` : '使用预设'
  return `${value.mode === 'fee' ? '低手续费优先 · 兜底' : '按品种规则'} · ${rules} · 默认${closePriorityLabel(value.fallback)}`
}
