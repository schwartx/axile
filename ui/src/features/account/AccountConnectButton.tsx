import { InkRewrite } from '@/components/ui/InkRewrite'
import { useConnectionOperation, useConnectionOperations } from '@/stores/connectionOperations'
import { useAccountConnection, useLiveConnection } from '@/stores/liveConnection'

/** 独立的连接动作；状态标签仍只负责打开连接详情。 */
export function AccountConnectButton({ accountId, prominent = false }: { accountId: number; prominent?: boolean }) {
  const connection = useAccountConnection(accountId)
  const synced = useLiveConnection(state => state.connected)
  const { pending } = useConnectionOperation(accountId)
  const busy = pending !== null || connection.status === 'connecting' || connection.status === 'recovering'
  const first = connection.status === 'uninitialized' || connection.status === 'disconnected'
  return <button type="button" disabled={!synced || busy}
    onClick={() => void useConnectionOperations.getState().run(accountId, 'reconnect')}
    className={`inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-lg px-3 py-2 text-[13px] disabled:cursor-default disabled:text-ink-3 ${prominent ? 'bg-ink-1 text-surface disabled:bg-fill' : 'text-accent hover:bg-fill'}`}>
    <Link2 size={14} className="shrink-0" aria-hidden />
    <InkRewrite text={pending === 'reconnect' || connection.status === 'connecting' || connection.status === 'recovering' ? '正在连接…' : first ? '连接账户' : '重新连接'} tone="label" />
  </button>
}
import { Link2 } from 'lucide-react'
