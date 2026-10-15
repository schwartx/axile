import { useServiceDisconnected } from '@/lib/errorPresentation'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'
import { useLayoutEffect, useRef, useState, useEffect } from 'react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { AccountConnectionProblemModal, type ConnectionSelection } from '@/features/account/AccountConnectionProblemModal'
import { connectionLabel } from '@/features/account/connectionProblem'
import { getAccountConnection } from '@/lib/api/accounts'
import { errorInfo } from '@/lib/errorInfo'
import type { AccountConnection, AccountConnectionDetail } from '@/types/api'

/** 标识只消费后端事实；打开后冻结结果，关闭/切换账户撤销在途请求。 */
export function AccountConnectionStatus({ accountId, connection }: { accountId: number; connection?: AccountConnection }) {
  const disconnected = useServiceDisconnected()
  const [selection, setSelection] = useState<ConnectionSelection | null>(null)
  const request = useRef<AbortController | null>(null)
  const sequence = useRef(0)
  const currentAccount = useRef(accountId)
  currentAccount.current = accountId
  useLayoutEffect(() => {
    request.current?.abort()
    request.current = null
    setSelection(null)
    return () => { request.current?.abort(); request.current = null }
  }, [accountId])
  const close = () => {
    request.current?.abort()
    request.current = null
    setSelection(null)
  }
  const current = selection?.kind === 'detail' ? selection.detail : connection
  const status = disconnected ? 'unknown' : current?.status ?? 'uninitialized'
  const label = disconnected ? '连接状态待更新' : connectionLabel(current)
  const stateName = { uninitialized: '未建立', unknown: '待确认', ready: '就绪', unavailable: '暂不可用', connecting: '准备中', recovering: '恢复中', disconnected: '已断开' }[status]
  const accessible = `账户连通状态：${stateName}${status === 'unavailable' && current?.label?.trim() ? `，${label}` : ''}`
  const open = async () => {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    const requestId = ++sequence.current
    setSelection({ kind: 'loading', accountId, requestId })
    const active = () => !controller.signal.aborted && request.current === controller && currentAccount.current === accountId
    try {
      const detail = await getAccountConnection(accountId, controller.signal)
      if (active()) setSelection({ kind: 'detail', accountId, requestId, detail })
    } catch (error) {
      if (active()) setSelection({ kind: 'failure', accountId, requestId, error, info: errorInfo(error, [{ label: '账户', value: String(accountId) }]) })
    }
  }
  useReadRecovery(open, selection?.kind === 'failure')
  const changed = (detail: AccountConnectionDetail) => setSelection(previous => previous?.accountId === accountId
    ? { kind: 'detail', accountId, requestId: ++sequence.current, detail } : previous)
  const modalAccount = selection?.accountId
  useEffect(() => {
    if (modalAccount === undefined || disconnected) return
    const controller = new AbortController()
    let reading = false
    const timer = window.setInterval(async () => {
      if (reading || document.hidden) return
      reading = true
      try {
        const detail = await getAccountConnection(modalAccount, controller.signal)
        if (!controller.signal.aborted) setSelection(previous => previous?.kind === 'detail' && previous.accountId === modalAccount
          ? { ...previous, detail } : previous)
      } catch {
        if (!controller.signal.aborted) setSelection(previous => previous?.kind === 'detail' && previous.accountId === modalAccount
          ? { ...previous, detail: { ...previous.detail, status: 'unknown', message: '当前会话状态暂时无法更新' } } : previous)
      }
      finally { reading = false }
    }, 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [modalAccount, disconnected])
  const chipClass = 'inline-flex min-w-0 max-w-full items-center rounded-chip bg-fill px-2.5 py-[3px] text-xs whitespace-nowrap'
  return <span className="inline-flex min-w-0 max-w-full" onClick={event => event.stopPropagation()}>
    <button type="button" aria-label={accessible} title={`查看${label}详情`} onClick={() => void open()}
      className={`${chipClass} cursor-pointer underline decoration-dashed decoration-current/40 underline-offset-4 hover:decoration-current focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${status === 'unavailable' ? 'text-warn' : status === 'ready' ? 'text-ink-2' : 'text-ink-3'}`}>
      <span role={status === 'ready' ? 'status' : undefined} aria-label={status === 'ready' ? accessible : undefined} className="min-w-0"><InkRewrite text={label} tone="label" overflow className="min-w-0" /></span>
    </button>
    <AccountConnectionProblemModal selection={selection?.accountId === accountId ? selection : null} onClose={close} onChanged={changed} />
  </span>
}
