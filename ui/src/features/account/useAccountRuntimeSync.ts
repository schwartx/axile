import { useCallback, useEffect, useRef, useState } from 'react'
import { getAccountRuntimeSync, retryAccountRuntimeSync } from '@/lib/api/accounts'
import type { AccountRuntimeSync } from '@/types/api'

export function runtimeSyncMessage(sync: AccountRuntimeSync | null | undefined): string {
  if (sync?.status === 'synchronized') return '算法配置已保存'
  const reason = sync?.last_error ? `：${sync.last_error}` : ''
  if (sync?.status === 'pending') return `配置已保存，运行态待同步${reason}`
  if (sync?.status === 'failed') return `配置已保存，运行态同步失败${reason}`
  return '配置已保存，同步状态暂不可用'
}

export function newerRuntimeSync(previous: AccountRuntimeSync | null, next: AccountRuntimeSync | null) {
  if (previous && (!next || next.revision < previous.revision)) return previous
  return next
}

/** 只对 pending 有限轮询；保存与重试使旧查询失效，不触碰算法草稿。 */
export function useAccountRuntimeSync(accountId: number) {
  const [owner, setOwner] = useState(accountId)
  const [data, setData] = useState<AccountRuntimeSync | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(false)
  const [queried, setQueried] = useState(false)
  const [deadline, setDeadline] = useState(0)
  const current = useRef({ accountId, generation: 0, controller: null as AbortController | null })

  current.current.accountId = accountId

  const invalidate = useCallback(() => {
    if (current.current.accountId !== accountId) return
    current.current.generation += 1
    current.current.controller?.abort()
    setLoading(false)
    setDeadline(0)
  }, [accountId])

  const run = useCallback(async (retry = false, restart = false) => {
    const request = current.current
    if (request.accountId !== accountId) return
    request.controller?.abort()
    const controller = new AbortController()
    request.controller = controller
    const generation = ++request.generation
    if (restart) setDeadline(Date.now() + 60_000)
    setLoading(true)
    setError(null)
    try {
      const next = await (retry ? retryAccountRuntimeSync : getAccountRuntimeSync)(accountId, controller.signal)
      if (generation !== request.generation || request.accountId !== accountId) return
      setData((previous) => newerRuntimeSync(previous, next))
      setQueried(true)
    } catch (cause) {
      if (generation !== request.generation || request.accountId !== accountId) return
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      if (generation === request.generation && request.accountId === accountId) setLoading(false)
    }
  }, [accountId])

  useEffect(() => {
    setOwner(accountId)
    setData(null)
    setQueried(false)
    void run(false, true)
    const request = current.current
    return () => {
      request.generation += 1
      request.controller?.abort()
    }
  }, [run, accountId])

  useEffect(() => {
    if (!queried || loading || error || (data && data.status !== 'pending') || Date.now() >= deadline) return
    const timer = window.setTimeout(() => {
      if (Date.now() < deadline) void run()
    }, 5000)
    return () => window.clearTimeout(timer)
  }, [queried, loading, error, data, deadline, run])

  const acceptSaved = useCallback((next: AccountRuntimeSync | null | undefined) => {
    if (current.current.accountId !== accountId) return
    invalidate()
    setError(null)
    setDeadline(Date.now() + 60_000)
    if (next) {
      setData((previous) => newerRuntimeSync(previous, next))
      setQueried(true)
    } else {
      setData(null)
      setQueried(false)
      void run(false, true)
    }
  }, [invalidate, run, accountId])

  const visible = owner === accountId
  return { data: visible ? data : null, error: visible ? error : null, loading: visible ? loading : true, queried: visible && queried, invalidate, acceptSaved,
    refresh: () => run(false, true), retry: () => run(true, true) }
}
