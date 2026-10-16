import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { ErrorDetailsModal } from '@/components/ui/ErrorDetailsModal'
import { Tooltip } from '@/components/ui/Tooltip'
import { errorInfo, isConnectionError, shortErrorReason, type ErrorInfo } from '@/lib/errorInfo'
import { timeAgo } from '@/lib/format'
import { useDomainStore } from '@/stores/domain'
import { useLiveConnection } from '@/stores/liveConnection'

/** 后台暂停与短暂重连保持安静，只在前台持续未同步时提示。 */
function useSyncInterrupted() {
  const synced = useLiveConnection(state => state.connected)
  const [interrupted, setInterrupted] = useState(false)
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined
    const check = () => {
      clearTimeout(timer)
      setInterrupted(false)
      if (!document.hidden && !synced) timer = setTimeout(() => setInterrupted(true), 5000)
    }
    check()
    document.addEventListener('visibilitychange', check)
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', check) }
  }, [synced])
  return !synced && interrupted
}

/** 由 dashboard 的真实读取驱动；外壳和设置向导共用唯一的连接入口。 */
export function ServiceConnectionStatus() {
  const updatedAt = useDomainStore((state) => state.accountsUpdatedAt)
  const error = useDomainStore((state) => state.accountsError)
  const refresh = useDomainStore((state) => state.refreshAccounts)
  const syncInterrupted = useSyncInterrupted()
  const [selection, setSelection] = useState<{ title: string; info: ErrorInfo } | null>(null)
  const [, tick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => tick(value => value + 1), 1000)
    return () => clearInterval(timer)
  }, [])
  const connecting = updatedAt == null && error == null
  const title = isConnectionError(error) ? '与服务器失联' : '账户数据刷新失败'
  const text = error ? `${title} · ${shortErrorReason(error)}`
    : connecting ? '正在连接' : syncInterrupted ? '实时状态同步中断，正在重新同步' : `数据 ${timeAgo(updatedAt)} · 服务正常`
  return <>
    <span role="status" className="flex min-w-0 items-center gap-1.5 text-[14px] text-ink-2">
      <span aria-hidden className={`h-2 w-2 flex-none rounded-full ${error || syncInterrupted
        ? 'bg-warn shadow-[0_0_0_3px_var(--color-warn-soft)]'
        : `bg-accent shadow-[0_0_0_3px_var(--color-accent-soft)]${connecting ? ' animate-pulse motion-reduce:animate-none' : ''}`}`} />
      {error ? <button type="button" aria-label="查看服务连接错误详情" className="cursor-pointer text-left hover:text-ink-1"
        onClick={() => setSelection({ title, info: errorInfo(error) })}>{text}</button> : text}
    </span>
    {error && <Tooltip content={`${shortErrorReason(error)}；点击重试`}>
      <button type="button" aria-label="重试连接服务器" className="cursor-pointer text-ink-3 hover:text-warn" onClick={() => void refresh()}>
        <RefreshCw size={14} aria-hidden />
      </button>
    </Tooltip>}
    <ErrorDetailsModal selection={selection} onClose={() => setSelection(null)} />
  </>
}
