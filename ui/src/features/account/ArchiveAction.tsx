import { useRef, useState } from 'react'
import { Archive, ArchiveRestore } from 'lucide-react'
import { ConfirmModal, type ConfirmSpec } from '@/components/ui/ConfirmModal'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { setAccountArchived } from '@/lib/api/accounts'
import { setPortfolioArchived } from '@/lib/api/portfolios'
import { useToastStore } from '@/stores/ui'

/** 账户和组合共用归档确认；服务端负责准入及运行态同步。 */
export function ArchiveAction({ kind, id, name, archivedAt, onChanged, className }: {
  kind: 'account' | 'portfolio'
  id: number
  name: string
  archivedAt?: string | null
  onChanged: () => void | Promise<void>
  className?: string
}) {
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [busy, setBusy] = useState(false)
  const locked = useRef(false)
  const toast = useToastStore(s => s.toast)
  const archived = archivedAt != null
  const noun = kind === 'account' ? '账户' : '组合'
  const verb = archived ? '恢复' : '归档'
  const Icon = archived ? ArchiveRestore : Archive

  const change = async () => {
    if (locked.current) return
    locked.current = true
    setBusy(true)
    setError(null)
    try {
      if (kind === 'account') {
        const result = await setAccountArchived(id, !archived)
        toast(result.runtime_sync && result.runtime_sync.status !== 'synchronized'
          ? `${noun}已${verb}，运行态待同步` : `${noun}已${verb}`)
      } else {
        await setPortfolioArchived(id, !archived)
        toast(`${noun}已${verb}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      try { await onChanged() } finally {
        locked.current = false
        setBusy(false)
      }
    }
  }

  return <div>
    <button type="button" disabled={busy}
      className={className ?? 'inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-sm text-ink-2 hover:bg-fill disabled:opacity-40'}
      onClick={() => setConfirm({
        title: `${verb}${noun}`,
        body: archived
          ? `恢复「${name}」到当前列表。${kind === 'account' ? '账户保持暂停，不自动连接或交易。' : '不会启动绑定账户。'}`
          : `归档「${name}」，保留配置和历史。${kind === 'account' ? '需先暂停并处理完在途及补单任务。归档不会平仓或撤单，保存的快照不代表实时持仓。' : '需先改绑、解绑或归档仍在使用它的账户。'}`,
        okText: verb,
        onConfirm: () => void change(),
      })}>
      <Icon size={16} className="shrink-0" aria-hidden /><span>{busy ? `${verb}中…` : `${verb}${noun}`}</span>
    </button>
    <ErrorNotice intent="mutation" title={`${verb}${noun}失败`} error={error} variant="compact" />
    <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />
  </div>
}
