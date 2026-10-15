import { describe, expect, it } from 'bun:test'

import { pollingView } from '@/lib/hooks/usePolling'

describe('pollingView', () => {
  const ready = {
    key: 'account:1',
    data: { value: 1 },
    error: null,
    loading: false,
    refreshing: false,
    updatedAt: 123,
  }

  it('新 queryKey 首帧隐藏旧实体并进入冷加载', () => {
    expect(pollingView(ready, 'account:2', true)).toEqual({
      data: null,
      error: null,
      loading: true,
      refreshing: false,
      updatedAt: null,
      stale: false,
    })
  })

  it('同 queryKey 后台刷新保留旧数据', () => {
    expect(pollingView({ ...ready, refreshing: true }, 'account:1', true)).toEqual({
      data: { value: 1 },
      error: null,
      loading: false,
      refreshing: true,
      updatedAt: 123,
      stale: false,
    })
  })

  it('已有数据后的刷新错误标记为 stale', () => {
    expect(pollingView({ ...ready, error: new Error('断线') }, 'account:1', true)).toEqual({
      data: { value: 1 },
      error: expect.any(Error),
      loading: false,
      refreshing: false,
      updatedAt: 123,
      stale: true,
    })
  })

  it('disabled 查询不暴露缓存，也不声称正在加载', () => {
    expect(pollingView(ready, 'account:1', false)).toEqual({
      data: null,
      error: null,
      loading: false,
      refreshing: false,
      updatedAt: null,
      stale: false,
    })
  })

  it('手动查询在首次读取或切换实体前保持未读取状态', () => {
    expect(pollingView(ready, 'account:2', true, true)).toEqual({
      data: null,
      error: null,
      loading: false,
      refreshing: false,
      updatedAt: null,
      stale: false,
    })
  })
})
