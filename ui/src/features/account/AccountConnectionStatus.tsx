import { useLayoutEffect, useRef, useState } from 'react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { AccountConnectionProblemModal, type ConnectionSelection } from '@/features/account/AccountConnectionProblemModal'
import { connectionLabel } from '@/features/account/connectionProblem'
import { getAccountConnection } from '@/lib/api/accounts'
import { errorInfo } from '@/lib/errorInfo'
import type { AccountConnection } from '@/types/api'

/** 标识只消费后端事实；打开后冻结结果，关闭/切换账户撤销在途请求。 */
export function AccountConnectionStatus({ accountId, connection }: { accountId: number; connection?: AccountConnection }) {
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
  const status = connection?.status ?? 'unknown'
  const label = connectionLabel(connection)
  const stateName = { unknown: '未知', ok: '正常', error: '异常' }[status]
  const accessible = `账户连通状态：${stateName}${status === 'error' && connection?.label?.trim() ? `，${label}` : ''}`
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
      if (active()) setSelection({ kind: 'failure', accountId, requestId, info: errorInfo(error, [{ label: '账户', value: String(accountId) }]) })
    }
  }
  const chipClass = 'inline-flex min-w-0 max-w-full items-center rounded-chip bg-fill px-2.5 py-[3px] text-xs whitespace-nowrap'
  return <span className="inline-flex min-w-0 max-w-full" onClick={event => { if (status !== 'ok') event.stopPropagation() }}>
    {status === 'ok' ? <span role="status" aria-label={accessible} className={`${chipClass} text-ink-2`}>
      <InkRewrite text={label} tone="label" overflow className="min-w-0" />
    </span> : <button type="button" aria-label={accessible} title={`查看${label}详情`} onClick={() => void open()}
      className={`${chipClass} cursor-pointer underline decoration-dashed decoration-current/40 underline-offset-4 hover:decoration-current focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${status === 'error' ? 'text-warn' : 'text-ink-3'}`}>
      <InkRewrite text={label} tone="label" overflow className="min-w-0" />
    </button>}
    <AccountConnectionProblemModal selection={selection?.accountId === accountId ? selection : null} onClose={close} />
  </span>
}
