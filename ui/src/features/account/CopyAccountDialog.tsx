import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from '@/components/ui/nav'
import { copyAccount } from '@/lib/api/accounts'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useDomainStore } from '@/stores/domain'

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
  const [copyName, setCopyName] = useState('')
  const [copying, setCopying] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const submitting = useRef(false)

  useEffect(() => {
    if (open) {
      setCopyName(`${name}（新）`)
      setError(null)
    }
  }, [open, name])

  const submit = async () => {
    if (submitting.current || !copyName.trim() || isStarted || isBusy) return
    submitting.current = true
    setCopying(true)
    setError(null)
    try {
      const created = await copyAccount(accountId, copyName.trim())
      await refreshAccounts()
      onClose()
      navigate(`/accounts/${created.id}`)
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
      <div className="w-full max-w-md rounded-xl border border-line bg-surface p-6 shadow-xl" role="dialog" aria-modal="true" aria-label="复制账户">
        <h2 className="text-lg font-semibold">复制账户</h2>
        <p className="mt-2 text-sm text-ink-2">仅复制配置和当前组合绑定，不复制执行历史，新账户默认暂停。旧账户历史保留；新收益曲线从新账户自身首个有效资产观测开始。</p>
        <p className="mt-2 text-sm text-ink-3">暂停只停止自动调度；旧账户仍可手动执行或重新启动，后续记录继续写入旧账户。</p>
        <label className="mt-4 block text-sm text-ink-2" htmlFor={`copy-account-name-${accountId}`}>新账户名称</label>
        <input id={`copy-account-name-${accountId}`} className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-ink-1" value={copyName} onChange={(event) => setCopyName(event.target.value)} />
        {isStarted && <p className="mt-3 text-sm text-warn">请先暂停旧账户的自动调度。</p>}
        {isBusy && <p className="mt-3 text-sm text-warn">请等待当前执行及排队任务结束。</p>}
        <ErrorNotice intent="mutation" title="复制账户失败" error={error} embedded onRetry={submit} contextKey={accountId} />
        <div className="mt-5 flex justify-end gap-3">
          <button type="button" className="rounded-lg px-3 py-2 text-ink-2 hover:bg-fill" disabled={copying} onClick={onClose}>取消</button>
          <button type="button" className="rounded-lg bg-ink-1 px-3 py-2 text-surface disabled:opacity-40" disabled={copying || isStarted || isBusy || !copyName.trim()} onClick={() => void submit()}>{copying ? '复制中…' : '创建暂停账户'}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
