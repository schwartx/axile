import { ConnectionProgressBar } from '@/features/account/connectionProgress/ConnectionProgressView'
import { useLayoutEffect, useState } from 'react'
import { Info } from 'lucide-react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { AccountConnectionDetailModal } from '@/features/account/AccountConnectionDetailModal'
import { useConnectionFeedback } from '@/features/account/useConnectionFeedback'
import { connectionLabel } from '@/features/account/connectionProblem'
import { useAccountConnectionSnapshot, useLiveConnection } from '@/stores/liveConnection'
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
  const connection = useAccountConnectionSnapshot(accountId)
  const operation = useConnectionOperation(accountId)
  const synced = useLiveConnection(state => state.connected)
  const [openFor, setOpenFor] = useState<number | null>(null)
  const [requestId, setRequestId] = useState(0)
  useLayoutEffect(() => { setOpenFor(null) }, [accountId])
  const feedback = useConnectionFeedback(accountId)
  const warning = connection.status === 'unavailable' || feedback.error !== null
  const status = connection.status
  const connected = status === 'ready'
  const progress = connection.progress
  const progressing = synced && !['unknown', 'disconnected', 'ready'].includes(status) && progress?.status === 'running'
  const currentStep = progressing ? progress.steps.find(step => step.status === 'running')?.label : null
  const busy = progressing || operation.pending !== null || (synced && (status === 'connecting' || status === 'recovering'))
  const phase = operation.pending === 'disconnect' ? 'disconnecting'
    : operation.pending === 'reconnect' || progressing || (synced && (status === 'connecting' || status === 'recovering')) ? 'connecting'
      : connected ? 'closed' : 'open'
  const label = ['unavailable', 'connecting', 'recovering'].includes(connection.status) ? connectionLabel(connection) : feedback.label ?? connectionLabel(connection)
  const switchLabel = operation.pending === 'disconnect' ? '断开中…' : currentStep ?? (operation.pending === 'reconnect' ? '连接中…' : label)

  const action = connected ? '断开连接' : '连接账户'
  const stateName = { uninitialized: '未建立', unknown: '待确认', ready: '就绪', unavailable: '暂不可用', connecting: '准备中', recovering: '恢复中', disconnected: '已断开' }[status]
  const accessible = `账户连通状态：${stateName}${status === 'unavailable' && connection.label?.trim() ? `，${label}` : ''}`
  const buttonClass = 'inline-flex min-w-0 items-center gap-1.5 rounded-chip px-2.5 py-1 text-xs hover:bg-fill focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
  return <div className="inline-flex min-w-0 max-w-full items-center gap-1" onClick={event => event.stopPropagation()}>
    <button type="button" role="switch" aria-checked={connected} aria-label="账户连接" aria-describedby={`connection-state-${accountId}`} aria-busy={busy}
      data-tone={busy ? 'busy' : warning ? 'warning' : connected ? 'ready' : 'idle'}
      title={busy ? switchLabel : !synced ? '连接状态待同步' : action} disabled={!synced || busy}
      onClick={() => void useConnectionOperations.getState().run(accountId, connected ? 'disconnect' : 'reconnect')}
      className={`${buttonClass} connection-toggle max-w-full cursor-pointer border active:bg-ink-3/10 disabled:cursor-default`}>
      <ConnectionIcon connected={connected} busy={busy} phase={phase} />
      <span id={`connection-state-${accountId}`} className="flex min-w-0 flex-col gap-1"><InkRewrite text={switchLabel} tone="prose" fluid overflow />
        {progressing && progress && <ConnectionProgressBar progress={progress} compact />}
      </span>
    </button>
    <button type="button" aria-label={`连接详情，${accessible}`} aria-haspopup="dialog" title="查看连接详情"
      onClick={() => { setRequestId(value => value + 1); setOpenFor(accountId) }}
      className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-chip hover:bg-fill focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent cursor-pointer ${warning ? 'text-warn' : 'text-ink-2'}`}>
      <Info size={14} className="shrink-0" aria-hidden />
    </button>
    <AccountConnectionDetailModal selection={openFor === accountId ? { accountId, requestId, detail: connection } : null} onClose={() => setOpenFor(null)} />
  </div>
}
