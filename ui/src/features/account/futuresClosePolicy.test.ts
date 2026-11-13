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

test('平今转锁仓默认开启，显式关闭不被默认值覆盖', () => {
  const policy = defaultClosePolicy()
  expect(policy.avoid_close_today).toEqual(['IC', 'IF', 'IH', 'IM'])
  policy.avoid_close_today = ['IC', 'IF']
  expect(closePolicySummary(policy)).toContain('IC、IF 平今转锁仓')
  policy.avoid_close_today = []
  expect(closePolicySummary(policy)).not.toContain('平今转锁仓')
  expect(defaultClosePolicy().avoid_close_today).toEqual(['IC', 'IF', 'IH', 'IM'])
})
