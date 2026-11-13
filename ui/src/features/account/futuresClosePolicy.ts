import type { FuturesClosePolicy, ClosePriority } from '@/types/api'

const DEFAULT_LOCK_PRODUCTS = ['IC', 'IF', 'IH', 'IM']
export const defaultClosePolicy = (): FuturesClosePolicy => ({ mode: 'rules', fallback: 'yesterday_first', overrides: {}, avoid_close_today: [...DEFAULT_LOCK_PRODUCTS] })
export const closePriorityLabel = (value: ClosePriority): string => value === 'today_first' ? '先今后昨' : '先昨后今'
export function closePolicySummary(value: FuturesClosePolicy): string {
  const count = Object.keys(value.overrides).length
  const rules = count ? `${count} 个品种已修改` : '使用预设'
  const products = value.avoid_close_today ?? DEFAULT_LOCK_PRODUCTS
  const locked = products.length ? ` · ${products.join('、')} 平今转锁仓` : ''
  return `${value.mode === 'fee' ? '低手续费优先 · 兜底' : '按品种规则'} · ${rules} · 默认${closePriorityLabel(value.fallback)}${locked}`
}
