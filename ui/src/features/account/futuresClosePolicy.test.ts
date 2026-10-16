import { expect, test } from 'bun:test'
import { defaultClosePolicy, closePolicySummary } from './futuresClosePolicy'

test('新账户默认按品种规则，摘要区分人工干预与自动兜底', () => {
  const policy = defaultClosePolicy()
  expect(policy.mode).toBe('rules')
  expect(closePolicySummary(policy)).toContain('使用预设')
  policy.overrides.AG = 'today_first'
  expect(closePolicySummary(policy)).toContain('1 个品种已修改')
  policy.mode = 'fee'
  expect(closePolicySummary(policy)).toContain('低手续费优先 · 兜底')
  expect(defaultClosePolicy().overrides).toEqual({})
})
