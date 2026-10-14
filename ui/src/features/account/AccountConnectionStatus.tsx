import { useEffect, useRef, useState } from 'react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { ErrorDetailsModal } from '@/components/ui/ErrorDetailsModal'
import { getAccountConnection } from '@/lib/api/accounts'
import { errorInfo, errorInfoFromEntries, errorValueText, type ErrorInfo } from '@/lib/errorInfo'
import type { AccountConnection } from '@/types/api'

/** 标识只消费后端连接事实；详情点击时冻结，不从请求失败推断账户异常。 */
export function AccountConnectionStatus({ accountId, connection }: { accountId: number; connection?: AccountConnection }) {
  const [selection, setSelection] = useState<{ title: string; info: ErrorInfo } | null>(null)
  const [loading, setLoading] = useState(false)
  const request = useRef<AbortController | null>(null)
  useEffect(() => {
    setSelection(null)
    setLoading(false)
    return () => { request.current?.abort() }
  }, [accountId])
  const status = connection?.status ?? 'unknown'
  const label = { unknown: '未知', ok: '正常', error: '异常' }[status]
  const open = async () => {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    try {
      const result = await getAccountConnection(accountId, controller.signal)
      if (controller.signal.aborted) return
      const evidence = [{ label: '账户', value: String(accountId) }, { label: '连通状态', value: { unknown: '未知', ok: '正常', error: '异常' }[result.status] },
        { label: '最近确认', value: result.observed_at ?? '尚未确认' }]
      const details = Object.entries(result.error ?? {}).map(([key, value]) => ({ label: key, value: errorValueText(value) }))
      setSelection({ title: '账户连接详情', info: errorInfoFromEntries(result.message ?? label, evidence, details) })
    } catch (error) {
      if (!controller.signal.aborted) setSelection({ title: '账户连接详情读取失败', info: errorInfo(error, [{ label: '账户', value: String(accountId) }]) })
    } finally { if (!controller.signal.aborted) setLoading(false) }
  }
  return <span className="inline-flex" onClick={(event) => event.stopPropagation()}>
    <button type="button" aria-label={`账户连通状态：${label}`} title={connection?.message ?? '查看账户连接详情'} disabled={loading} onClick={() => void open()} className={`inline-flex items-center rounded-chip bg-fill px-2.5 py-[3px] text-xs whitespace-nowrap cursor-pointer underline decoration-dashed decoration-current/40 underline-offset-4 hover:decoration-current focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-40 ${status === 'error' ? 'text-warn' : status === 'ok' ? 'text-ink-2' : 'text-ink-3'}`}>
      <InkRewrite text={`连接${label}`} tone="label" />
    </button>
    <ErrorDetailsModal selection={selection} onClose={() => setSelection(null)} />
  </span>
}
