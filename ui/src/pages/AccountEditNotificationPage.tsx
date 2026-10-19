/** 账户自定义执行通知工作台：左侧试跑，右侧编辑代码。 */

import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { usePythonDraft, usePythonRun, usePythonSave } from '@/components/ui/pythonWorkbenchState'
import { NotificationWorkbench } from '@/features/notification/NotificationWorkbench'
import { AccountPageTitle } from '@/features/account/pageHead'
import { EditError, EditLoading } from '@/features/account/editUi'
import { notificationDraft } from '@/features/account/notificationDraft'
import { NOTIFICATION_STATUS_LABEL } from '@/features/account/notificationStatus'
import { getAccount, getDefaultAccountNotification, testAccountNotificationFunction, updateAccount, type AccountFeishuTestResult } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'

const STORAGE_KEY = 'axon.notificationWorkbench'

export function AccountEditNotificationPage() {
  const { id } = useParams()
  const accountId = Number(id)
  const toast = useToastStore((state) => state.toast)
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const account = usePolling(useCallback((signal: AbortSignal) => getAccount(accountId, signal), [accountId]), {
    queryKey: `account:${accountId}`,
    intervalMs: 0,
  })
  const draftState = usePythonDraft<string | null>(null)
  const { draft: code, setDraft: setCode, setBaseline: setSavedCode } = draftState
  const saveState = usePythonSave()
  const clearSaveError = saveState.clearError
  useEffect(() => {
    setCode(null)
    setSavedCode(null)
    clearSaveError()
  }, [accountId, setCode, setSavedCode, clearSaveError])
  const template = usePolling(useCallback((signal: AbortSignal) => getDefaultAccountNotification(accountId, signal), [accountId]), {
    queryKey: `account:${accountId}:notification-template`, intervalMs: 0,
  })
  const defaultCode = template.data
  const templateError = template.error
  const templateLoading = template.loading || template.refreshing
  const loadTemplate = template.refresh

  useEffect(() => {
    if (account.data && defaultCode !== null && code === null) {
      const initial = notificationDraft(account.data.execution_notification_code, defaultCode)
      setCode(initial.code)
      setSavedCode(initial.savedCode)
    }
  }, [account.data, code, defaultCode, setCode, setSavedCode])

  const [eventType, setEventType] = useState<'execution.finished' | 'supplement.cancelled'>('execution.finished')
  const trial = usePythonRun<AccountFeishuTestResult>({
    code: code ?? '', enabled: !saveState.saving,
    execute: (draft) => testAccountNotificationFunction(accountId, draft, undefined, eventType),
    failed: (cause) => ({ ok: false, message: cause instanceof Error ? cause.message : String(cause) }),
    toEditorResult: (test) => ({ valid: test.ok, errorMessage: test.ok ? null : test.message, errorLine: test.error_line, traceback: test.traceback }),
  })
  const { result: test, editorResult: result, stale } = trial
  const busy = trial.running ? 'test' : saveState.saving ? 'save' : null
  const error = saveState.error

  if (account.error && !account.data) return <EditError error={account.error} onRetry={account.refresh} />
  if (templateError && code === null) return <section><ErrorNotice intent="read" title="默认模板读取失败" error={templateError} onRetry={() => void loadTemplate()} /></section>
  if (!account.data || code === null) return <EditLoading />

  const acc = account.data
  const dirty = draftState.dirty
  const savedNotificationStatus = NOTIFICATION_STATUS_LABEL[acc.execution_notification_status]
  const runTest = async () => {
    saveState.clearError()
    await trial.run()
  }
  const save = () => saveState.save(dirty && !trial.running, async () => {
    await updateAccount(accountId, { execution_notification_code: code.trim() ? code : null })
    setSavedCode(code)
    toast(code.trim() ? '执行通知函数已保存' : '执行通知已关闭')
    void refreshAccounts()
    void account.refresh()
  })
  const restore = () => {
    draftState.restore()
    saveState.clearError()
  }

  return (
    <NotificationWorkbench
      kind="account_notification"
      storageKey={STORAGE_KEY}
      header={<>
        <div className="flex flex-wrap items-center gap-3"><AccountPageTitle accountId={accountId} page="执行通知函数" name={acc.name} channel={acc.trade_channel} market={acc.market} /></div>
        <label className="mt-2 block text-[13px] text-ink-3">试跑事件 <select value={eventType} onChange={(event) => setEventType(event.target.value as typeof eventType)} disabled={trial.running}><option value="execution.finished">执行完成</option><option value="supplement.cancelled">补发取消</option></select></label>
        <p className="mt-2 text-[13px] text-ink-3">{savedNotificationStatus}</p>
      </>}
      code={code}
      onChange={(value) => { setCode(value); saveState.clearError() }}
      defaultCode={templateLoading ? null : defaultCode}
      onReset={() => { setCode(defaultCode); saveState.clearError() }}
      running={busy === 'test'} result={result} message={test?.message} stale={stale}
      onRun={() => void runTest()}
      saveAction={{ dirty, saving: saveState.saving, error, disabled: trial.running, onSave: () => void save(), onRestore: restore }}
      docHref="/docs/notification"
    />
  )
}
