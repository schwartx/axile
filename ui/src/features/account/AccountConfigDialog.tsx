import { useState, type ReactNode } from 'react'

import { CenteredModal } from '@/components/ui/CenteredModal'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { updateAccount, type AccountUpdatePayload } from '@/lib/api/accounts'
import type { Account } from '@/types/api'

export function AccountConfigDialog({ accountId, accountName, title, patch, preparePatch, onClose, onSaved, children }: {
  accountId: number
  accountName: string
  title: string
  patch: AccountUpdatePayload | null
  preparePatch?: (patch: AccountUpdatePayload) => Promise<AccountUpdatePayload>
  onClose: () => void
  onSaved: (updated: Account) => void
  children: ReactNode
}) {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const canSave = !saving && patch !== null && Object.keys(patch).length > 0

  const save = async () => {
    if (!canSave) return
    setError(null)
    setSaving(true)
    try {
      const updated = await updateAccount(accountId, preparePatch ? await preparePatch(patch) : patch)
      onSaved(updated)
    } catch (cause) {
      setError(cause)
    } finally {
      setSaving(false)
    }
  }

  return (
    <CenteredModal
      open
      title={title}
      subtitle={accountName}
      onClose={onClose}
      closeDisabled={saving}
      widthClassName="w-[540px]"
      footer={
        <div className="mt-1 flex justify-end gap-2.5 border-t border-line px-[22px] py-3.5">
          <button type="button" onClick={onClose} disabled={saving}
            className="cursor-pointer rounded-[9px] border border-line bg-surface px-4 py-2 text-sm text-ink-2 disabled:opacity-45">
            取消
          </button>
          <button type="button" onClick={() => void save()} disabled={!canSave}
            className="cursor-pointer rounded-[9px] border border-ink-1 bg-ink-1 px-[18px] py-2 text-sm font-[550] text-surface disabled:opacity-45">
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      }
    >
      <fieldset disabled={saving} className="min-w-0 space-y-4 pt-1 pb-2">{children}</fieldset>
      <ErrorNotice title={`保存${title}失败`} intent="mutation" error={error} />
    </CenteredModal>
  )
}
