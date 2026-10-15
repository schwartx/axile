import { useLayoutEffect, useState } from 'react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { AccountConnectButton } from '@/features/account/AccountConnectButton'
import { AccountConnectionProblemModal } from '@/features/account/AccountConnectionProblemModal'
import { connectionLabel } from '@/features/account/connectionProblem'
import { useAccountConnection } from '@/stores/liveConnection'
import { useConnectionOperation } from '@/stores/connectionOperations'

/** 标识与弹窗共用实时连接状态，打开详情不触发查询或登录。 */
export function AccountConnectionStatus({ accountId }: { accountId: number }) {
  const connection = useAccountConnection(accountId)
  const operation = useConnectionOperation(accountId)
  const [openFor, setOpenFor] = useState<number | null>(null)
  const [requestId, setRequestId] = useState(0)
  useLayoutEffect(() => { setOpenFor(null) }, [accountId])
  const status = connection.status
  const label = connectionLabel(connection)
  const stateName = { uninitialized: '未建立', unknown: '待确认', ready: '就绪', unavailable: '暂不可用', connecting: '准备中', recovering: '恢复中', disconnected: '已断开' }[status]
  const accessible = `账户连通状态：${stateName}${status === 'unavailable' && connection.label?.trim() ? `，${label}` : ''}`
  const chipClass = 'inline-flex min-w-0 max-w-full items-center rounded-chip bg-fill px-2.5 py-[3px] text-xs whitespace-nowrap'
  return <div className="inline-flex min-w-0 max-w-full flex-col items-start" onClick={event => event.stopPropagation()}>
    <div className="flex min-w-0 max-w-full flex-wrap items-center gap-x-1">
      <button type="button" aria-label={accessible} title={`查看${label}详情`} onClick={() => { setRequestId(value => value + 1); setOpenFor(accountId) }}
        className={`${chipClass} cursor-pointer underline decoration-dashed decoration-current/40 underline-offset-4 hover:decoration-current focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${status === 'unavailable' ? 'text-warn' : status === 'ready' ? 'text-ink-2' : 'text-ink-3'}`}>
        <span role={status === 'ready' ? 'status' : undefined} aria-label={status === 'ready' ? accessible : undefined} className="min-w-0"><InkRewrite text={label} tone="label" overflow className="min-w-0" /></span>
      </button>
      <span inert={status === 'ready'} aria-hidden={status === 'ready'}
        className={`grid transition-[grid-template-columns] duration-200 motion-reduce:transition-none ${status === 'ready' ? 'grid-cols-[0fr]' : 'grid-cols-[1fr]'}`}>
        <span className="min-w-0 overflow-hidden"><AccountConnectButton accountId={accountId} /></span>
      </span>
    </div>
    {/* 错误槽只跟随控件行宽度，收起后保留的内容不能撑开账户标题行。 */}
    <div className="w-0 min-w-full">
      <ErrorNotice error={openFor !== accountId ? operation.error : null} title="连接操作未完成" intent="mutation" variant="compact" />
    </div>
    <AccountConnectionProblemModal selection={openFor === accountId ? { accountId, requestId, detail: connection } : null} onClose={() => setOpenFor(null)} />
  </div>
}
