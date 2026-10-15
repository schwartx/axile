import type { ReactNode } from 'react'
import { Unlink } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { AccountConnectButton } from '@/features/account/AccountConnectButton'
import { useConnectionOperation, useConnectionOperations } from '@/stores/connectionOperations'
import { useLiveConnection } from '@/stores/liveConnection'
import type { AccountConnectionDetail } from '@/types/api'

/** 弹窗与外部入口共享连接操作，结果状态只消费 SSE。 */
export function AccountConnectionActions({ accountId, detail, children }: {
  accountId: number; detail: AccountConnectionDetail; children?: ReactNode
}) {
  const { pending, error } = useConnectionOperation(accountId)
  const synced = useLiveConnection(state => state.connected)
  const busy = !synced || pending !== null || detail.status === 'connecting' || detail.status === 'recovering'
  const needsConnection = detail.status === 'disconnected' || detail.status === 'uninitialized'
  return <section className="space-y-2 border-t border-line px-[22px] py-3 text-[13px]">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex flex-wrap gap-2">
        <AccountConnectButton accountId={accountId} prominent={needsConnection} />
        {!needsConnection && <button type="button" disabled={busy} onClick={() => void useConnectionOperations.getState().run(accountId, 'disconnect')}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-ink-2 disabled:cursor-default disabled:text-ink-3">
          <Unlink size={14} className="shrink-0" aria-hidden />
          <InkRewrite text={pending === 'disconnect' ? '正在断开…' : '断开连接'} tone="label" />
        </button>}
      </div>
      <div className="ml-auto flex flex-wrap items-center gap-2">{children}</div>
    </div>
    <ErrorNotice error={error} title="连接操作未完成，请核对当前状态" embedded intent="uncertain" />
  </section>
}
