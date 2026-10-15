import { useLayoutEffect, useState } from 'react'
import { Info } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { OverflowText } from '@/components/ui/OverflowText'
import { AccountConnectionProblemModal } from '@/features/account/AccountConnectionProblemModal'
import { connectionLabel } from '@/features/account/connectionProblem'
import { useAccountConnection, useLiveConnection } from '@/stores/liveConnection'
import { useConnectionOperation, useConnectionOperations } from '@/stores/connectionOperations'

/** 同一对链环单次分合，在途停在中间态，确认成功后落位。 */
function ConnectionIcon({ connected, busy, phase }: { connected: boolean; busy: boolean; phase: 'closed' | 'open' | 'connecting' | 'disconnecting' }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true" className="connection-link shrink-0" data-connected={connected} data-busy={busy} data-phase={phase}>
    <g className="connection-link-start"><path d="M10 14H8a4 4 0 0 1 0-8h2a4 4 0 0 1 4 4" /></g>
    <g className="connection-link-end"><path d="M14 10h2a4 4 0 0 1 0 8h-2a4 4 0 0 1-4-4" /></g>
    <path className="connection-link-join" d="m9 15 6-6" />
  </svg>
}

/** 连接开关与详情入口独立，共享 SSE 状态和在途操作。 */
export function AccountConnectionStatus({ accountId }: { accountId: number }) {
  const connection = useAccountConnection(accountId)
  const operation = useConnectionOperation(accountId)
  const synced = useLiveConnection(state => state.connected)
  const [openFor, setOpenFor] = useState<number | null>(null)
  const [requestId, setRequestId] = useState(0)
  useLayoutEffect(() => { setOpenFor(null) }, [accountId])
  const status = connection.status
  const connected = status === 'ready'
  const busy = operation.pending !== null || status === 'connecting' || status === 'recovering'
  const phase = operation.pending === 'disconnect' ? 'disconnecting'
    : operation.pending === 'reconnect' || status === 'connecting' || status === 'recovering' ? 'connecting'
      : connected ? 'closed' : 'open'
  const label = connectionLabel(connection)
  const switchLabel = operation.pending === 'disconnect' ? '断开中…' : operation.pending === 'reconnect' ? '连接中…'
    : status === 'unavailable' || status === 'uninitialized' ? '未连接' : label
  const action = connected ? '断开连接' : '连接账户'
  const detailLabel = status === 'unavailable' ? label : '连接详情'
  const stateName = { uninitialized: '未建立', unknown: '待确认', ready: '就绪', unavailable: '暂不可用', connecting: '准备中', recovering: '恢复中', disconnected: '已断开' }[status]
  const accessible = `账户连通状态：${stateName}${status === 'unavailable' && connection.label?.trim() ? `，${label}` : ''}`
  const buttonClass = 'inline-flex min-w-0 items-center gap-1.5 rounded-chip px-2.5 py-1 text-xs hover:bg-fill focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
  return <div className="inline-flex min-w-0 max-w-full flex-col items-start" onClick={event => event.stopPropagation()}>
    <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
      <button type="button" role="switch" aria-checked={connected} aria-label="账户连接" aria-describedby={`connection-state-${accountId}`} aria-busy={busy}
        title={busy ? switchLabel : !synced ? '连接状态待同步' : action} disabled={!synced || busy}
        onClick={() => void useConnectionOperations.getState().run(accountId, connected ? 'disconnect' : 'reconnect')}
        className={`${buttonClass} shrink-0 cursor-pointer border border-line hover:border-border-strong active:bg-ink-3/10 text-ink-2 disabled:cursor-default disabled:text-ink-3`}>
        <ConnectionIcon connected={connected} busy={busy} phase={phase} />
        <span id={`connection-state-${accountId}`}>{switchLabel}</span>
      </button>
      <button type="button" aria-label={`连接详情，${accessible}`} aria-haspopup="dialog" title="查看连接详情"
        onClick={() => { setRequestId(value => value + 1); setOpenFor(accountId) }}
        className={`${buttonClass} border border-line hover:border-border-strong max-w-full cursor-pointer ${status === 'unavailable' ? 'text-warn' : 'text-ink-2'}`}>
        <Info size={14} className="shrink-0" aria-hidden />
        <OverflowText text={detailLabel} className="min-w-0" />
      </button>
    </div>
    <div className="w-0 min-w-full">
      <ErrorNotice error={openFor !== accountId ? operation.error : null} title="连接操作未完成" intent="mutation" variant="compact" />
    </div>
    <AccountConnectionProblemModal selection={openFor === accountId ? { accountId, requestId, detail: connection } : null} onClose={() => setOpenFor(null)} />
  </div>
}
