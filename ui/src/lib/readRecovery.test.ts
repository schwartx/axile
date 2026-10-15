import { expect, it } from 'bun:test'
import { notifyReadRecovery, subscribeReadRecovery } from '@/lib/readRecovery'
import { useDomainStore } from '@/stores/domain'
import { ApiError } from '@/lib/api/client'

it('恢复读取相互独立，卸载订阅不再参与恢复', async () => {
  const calls: string[] = []
  const removed = subscribeReadRecovery(() => { calls.push('卸载') })
  const failed = subscribeReadRecovery(() => { calls.push('失败'); throw new Error('仍不可用') })
  const healthy = subscribeReadRecovery(async () => { calls.push('成功') })
  removed()
  try {
    await notifyReadRecovery()
    expect(calls).toEqual(['失败', '成功'])
  } finally { failed(); healthy() }
})

it('仅 dashboard 从网络失败恢复为成功刷新时通知一次', async () => {
  const previous = useDomainStore.getState()
  let calls = 0
  const unsubscribe = subscribeReadRecovery(() => { calls++ })
  try {
    useDomainStore.setState({ accountsError: new TypeError('Failed to fetch'), accountsUpdatedAt: 1 })
    useDomainStore.setState({ portfoliosError: null })
    expect(calls).toBe(0)
    useDomainStore.setState({ accountsError: new ApiError(500, 'HTTP 500') })
    expect(calls).toBe(0)
    useDomainStore.setState({ accountsError: new TypeError('Failed to fetch') })
    useDomainStore.setState({ accountsError: null, accountsUpdatedAt: 2 })
    expect(calls).toBe(1)
    useDomainStore.setState({ accountsError: null, accountsUpdatedAt: 3 })
    expect(calls).toBe(1)
  } finally { unsubscribe(); useDomainStore.setState(previous) }
})
