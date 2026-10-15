import { describe, expect, it } from 'bun:test'

import {
  countAccountControlOverrides,
  normalizedAccountControlOverride,
  normalizedOperationOverride,
  resolveAccountControlPolicy,
  sameAccountControlOverride,
} from './accountControlPolicy'
import type { AccountControlOverride, AccountControlPolicy } from '@/types/api'

describe('normalizedAccountControlOverride', () => {
  it('仅预警比例的覆盖可以保存，关闭时只移除预警、不丢失额度规则', () => {
    expect(normalizedOperationOverride({ daily_warning_percent: 80 })).toEqual({ daily_warning_percent: 80 })
    expect(normalizedOperationOverride({ daily_warning_percent: null, account: { per_day: { limit: 500 } } })).toEqual({ account: { per_day: { limit: 500 } } })
    expect(countAccountControlOverrides({ operations: { place_order: { daily_warning_percent: 80 } }, groups: {} })).toBe(1)
  })

  it('取消日额度限制时保留预警比例，恢复限额后继续使用', () => {
    const override: AccountControlOverride = { operations: { place_order: { daily_warning_percent: 90, account: { per_day: { unlimited: true } } } }, groups: {} }
    const base: AccountControlPolicy = { timezone: 'Asia/Shanghai', operations: { place_order: { priority: 100, account: { per_day: { limit: 500, on_trigger: 'block' } } } }, groups: {} }
    const resolved = resolveAccountControlPolicy(base, override).operations.place_order
    expect(resolved.daily_warning_percent).toBe(90)
    expect(resolved.account.per_day).toBeNull()
    expect(normalizedAccountControlOverride(override)?.operations.place_order.daily_warning_percent).toBe(90)
  })

  it('保留仅含 priority=0 的覆盖并移除服务端填充的 null', () => {
    const value: AccountControlOverride = {
      timezone: null,
      operations: {
        cancel_order: { priority: 0, account: null, symbol: null },
      },
      groups: {},
    }

    expect(normalizedAccountControlOverride(value)).toEqual({
      operations: { cancel_order: { priority: 0 } },
      groups: {},
    })
  })

  it('修改其他规则时不丢失操作优先级', () => {
    expect(normalizedOperationOverride({
      priority: 10,
      account: { per_day: { limit: 500, on_trigger: 'block' } },
      symbol: null,
    })).toEqual({
      priority: 10,
      account: { per_day: { limit: 500, on_trigger: 'block' } },
    })
  })

  it('空覆盖归一化为 null', () => {
    expect(normalizedAccountControlOverride({
      timezone: null,
      operations: { query_order: { priority: null, account: null, symbol: null } },
      groups: {},
    })).toBeNull()
  })
})

describe('sameAccountControlOverride', () => {
  it('忽略 null 填充和对象键顺序，避免页面初始即显示待保存', () => {
    const serverValue: AccountControlOverride = {
      timezone: null,
      operations: {
        place_order: {
          priority: null,
          account: {
            per_minute: null,
            per_day: { limit: 12, on_trigger: 'block' },
            min_interval_ms: null,
          },
          symbol: null,
        },
      },
      groups: {},
    }
    const editorValue: AccountControlOverride = {
      operations: {
        place_order: { account: { per_day: { on_trigger: 'block', limit: 12 } } },
      },
      groups: {},
    }

    expect(sameAccountControlOverride(serverValue, editorValue)).toBe(true)
  })
})

describe('account control priority', () => {
  const base: AccountControlPolicy = {
    timezone: 'Asia/Shanghai',
    operations: {
      cancel_order: { priority: 0, account: {} },
      query_order: { priority: 100, account: {} },
    },
    groups: {},
  }

  it('覆盖参与有效策略合并并按一处自定义计数', () => {
    const override: AccountControlOverride = {
      operations: {
        query_order: {
          priority: 20,
          account: { per_minute: { limit: 30, on_trigger: 'wait' } },
        },
      },
      groups: {},
    }

    expect(resolveAccountControlPolicy(base, override).operations.query_order.priority).toBe(20)
    expect(countAccountControlOverrides(override)).toBe(2)
  })

  it('priority=0 不会被空值判断误删', () => {
    const override: AccountControlOverride = {
      operations: { query_order: { priority: 0 } },
      groups: {},
    }

    expect(resolveAccountControlPolicy(base, override).operations.query_order.priority).toBe(0)
    expect(countAccountControlOverrides(override)).toBe(1)
  })
})

describe('unlimited 规则覆盖', () => {
  const base: AccountControlPolicy = {
    timezone: 'Asia/Shanghai',
    operations: {
      place_order: {
        priority: 10,
        account: {
          per_minute: { limit: 30, on_trigger: 'wait' },
          per_day: { limit: 500, on_trigger: 'block' },
          min_interval_ms: { limit: 500, on_trigger: 'wait' },
        },
      },
    },
    groups: {},
  }

  it('unlimited 解析为无限制，且与后端一致不受基线影响', () => {
    const override: AccountControlOverride = {
      operations: {
        place_order: { account: { per_day: { unlimited: true } } },
      },
      groups: {},
    }
    const resolved = resolveAccountControlPolicy(base, override).operations.place_order.account

    expect(resolved.per_day).toBeNull()
    expect(resolved.per_minute).toEqual({ limit: 30, on_trigger: 'wait' })
  })

  it('unlimited 归一化保留标记并参与计数与等价比较', () => {
    const override: AccountControlOverride = {
      operations: {
        place_order: { account: { per_day: { unlimited: true } } },
      },
      groups: {},
    }

    expect(normalizedAccountControlOverride(override)).toEqual({
      operations: { place_order: { account: { per_day: { unlimited: true } } } },
      groups: {},
    })
    expect(countAccountControlOverrides(override)).toBe(1)
    expect(sameAccountControlOverride(override, {
      operations: { place_order: { account: { per_day: { unlimited: true } } } },
      groups: {},
    })).toBe(true)
  })
})
