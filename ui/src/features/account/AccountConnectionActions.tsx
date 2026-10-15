import { useRef, useState } from 'react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { getAccountConnection, operateAccountConnection } from '@/lib/api/accounts'
import { useDomainStore } from '@/stores/domain'
import type { AccountConnectionDetail } from '@/types/api'

/** 连接操作只由明确点击触发；故障后的只读刷新不能重放写请求。 */
export function AccountConnectionActions({ accountId, detail, onChanged }: {
  accountId: number; detail: AccountConnectionDetail; onChanged?: (detail: AccountConnectionDetail) => void
}) {
  const [pending, setPending] = useState<'reconnect' | 'disconnect' | null>(null)
  const [error, setError] = useState<unknown>(null)
  const inFlight = useRef(false)
  const busy = pending !== null || detail.status === 'connecting' || detail.status === 'recovering'
  const run = async (action: 'reconnect' | 'disconnect') => {
    if (busy || inFlight.current) return
    inFlight.current = true
    setPending(action)
    setError(null)
    try {
      onChanged?.(await operateAccountConnection(accountId, action))
    } catch (failure) {
      setError(failure)
      // 只刷新结果证据；不重发连接操作。
      try { onChanged?.(await getAccountConnection(accountId)) } catch { /* 保留原操作错误 */ }
    } finally {
      inFlight.current = false
      setPending(null)
      void useDomainStore.getState().refreshAccounts()
    }
  }
  return <section className="space-y-3 border-t border-line pt-4">
    <p className="text-ink-3">连接与定时任务分别管理。恢复连接后会核对资产和委托，交易任务需另行执行。</p>
    <div className="flex flex-wrap gap-3">
      <button type="button" disabled={busy} onClick={() => void run('reconnect')}
        className="cursor-pointer rounded-lg border border-line px-3 py-2 text-accent disabled:cursor-default disabled:text-ink-3">
        <InkRewrite text={pending === 'reconnect' ? '正在连接…' : detail.status === 'disconnected' || detail.status === 'uninitialized' ? '连接账户' : '重新连接'} tone="label" />
      </button>
      <button type="button" disabled={busy || detail.status === 'disconnected' || detail.status === 'uninitialized'} onClick={() => void run('disconnect')}
        className="cursor-pointer rounded-lg border border-line px-3 py-2 text-ink-2 disabled:cursor-default disabled:text-ink-3">
        <InkRewrite text={pending === 'disconnect' ? '正在断开…' : '断开连接'} tone="label" />
      </button>
    </div>
    <ErrorNotice error={error} title="连接操作未完成，请核对当前状态" embedded intent="uncertain" />
  </section>
}
