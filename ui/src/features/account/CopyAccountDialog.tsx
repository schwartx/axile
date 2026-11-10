import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from '@/components/ui/nav'
import { copyAccount, setAccountArchived } from '@/lib/api/accounts'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'

interface CopyAccountDialogProps {
  accountId: number
  name: string
  isStarted: boolean
  isBusy: boolean
  open: boolean
  onClose: () => void
}

/** 列表和详情共用的账户复制确认与状态说明。 */
export function CopyAccountDialog({ accountId, name, isStarted, isBusy, open, onClose }: CopyAccountDialogProps) {
  const navigate = useNavigate()
  const refreshAccounts = useDomainStore((s) => s.refreshAccounts)
  const toast = useToastStore((s) => s.toast)
  const [copyName, setCopyName] = useState('')
  const [copying, setCopying] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [createdId, setCreatedId] = useState<number | null>(null)
  const [archiveOriginal, setArchiveOriginal] = useState(false)
  const submitting = useRef(false)

  useEffect(() => {
    if (open) {
      setCopyName(`${name}（新）`)
      setError(null)
      setCreatedId(null)
      setArchiveOriginal(false)
    }
  }, [open, name])

  const submit = async (archive = false) => {
    if (submitting.current || !copyName.trim() || isStarted || isBusy) return
    submitting.current = true
    setCopying(true)
    setError(null)
    setArchiveOriginal(archive)
    try {
      let targetId = createdId
      if (targetId == null) {
        const created = await copyAccount(accountId, copyName.trim())
        targetId = created.id!
        setCreatedId(targetId)
      }
      if (archive) {
        const original = await setAccountArchived(accountId, true)
        toast(original.runtime_sync && original.runtime_sync.status !== 'synchronized'
          ? '新账户已创建，原账户已归档，运行态待同步' : '新账户已创建，原账户已归档')
      }
      await refreshAccounts()
      onClose()
      navigate(`/accounts/${targetId}`)
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      submitting.current = false
      setCopying(false)
    }
  }

  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/45 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !copying) onClose() }}>
      <div className="max-h-[calc(100dvh-2rem)] w-full max-w-lg overflow-y-auto rounded-xl border border-line bg-surface p-6 shadow-xl" role="dialog" aria-modal="true" aria-label="复制账户">
        <h2 className="text-lg font-semibold">复制账户</h2>
        <p className="mt-2 text-sm text-ink-2">仅复制配置和当前组合绑定，不复制执行历史，新账户默认暂停。旧账户历史保留；新收益曲线从新账户自身首个有效资产观测开始。</p>
        <p className="mt-2 text-sm text-ink-3">暂停只停止自动调度；旧账户仍可手动执行或重新启动。选择“创建并归档原账户”后，原账户保留配置和历史，退出日常运行；归档不会平仓或撤单。</p>
        <label className="mt-4 block text-sm text-ink-2" htmlFor={`copy-account-name-${accountId}`}>新账户名称</label>
        <input id={`copy-account-name-${accountId}`} className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-ink-1" disabled={copying || createdId != null} value={copyName} onChange={(event) => setCopyName(event.target.value)} />
        {isStarted && <p className="mt-3 text-sm text-warn">请先暂停旧账户的自动调度。</p>}
        {isBusy && <p className="mt-3 text-sm text-warn">请等待当前执行及排队任务结束。</p>}
        {createdId != null && archiveOriginal && !copying && <p className="mt-3 text-sm text-ink-2">新账户已创建，原账户归档尚未确认。重试只归档原账户，不会重复创建。</p>}
        <ErrorNotice intent="mutation" title={createdId != null && archiveOriginal ? '归档原账户失败' : '复制账户失败'} error={error} embedded onRetry={() => submit(archiveOriginal)} contextKey={accountId} />
        <div className="mt-5 flex flex-wrap justify-end gap-3">
          <button type="button" className="rounded-lg px-3 py-2 text-ink-2 hover:bg-fill" disabled={copying} onClick={onClose}>取消</button>
          <button type="button" className="rounded-lg border border-line px-3 py-2 text-ink-2 hover:bg-fill disabled:opacity-40" disabled={copying || createdId != null || isStarted || isBusy || !copyName.trim()} onClick={() => void submit()}>{copying && !archiveOriginal ? '复制中…' : '创建暂停账户'}</button>
          <button type="button" className="rounded-lg bg-ink-1 px-3 py-2 text-surface disabled:opacity-40" disabled={copying || isStarted || isBusy || !copyName.trim()} onClick={() => void submit(true)}>{copying && archiveOriginal ? '处理中…' : createdId != null ? '重试归档原账户' : '创建并归档原账户'}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
