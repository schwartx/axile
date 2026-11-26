import { useState } from 'react'
import { assertPythonBaseline, usePythonSave } from '@/components/ui/pythonWorkbenchState'
import { EditSaveBar } from '@/features/account/editUi'
import { extractFeishuKey } from '@/features/account/feishuUpdate'
import { AccountPageTitle } from '@/features/account/pageHead'
import { draftKey } from '@/features/editor/drafts'
import { SignalNotificationSetup, type SignalNotificationMode } from '@/features/signal/SignalNotificationSetup'
import { SIGNAL_NOTIFICATION_EXAMPLE } from '@/features/signal/signalNotificationExample'
import {
  getAccount, getDefaultAccountNotification, testAccountFeishu, testAccountNotificationFunction,
  updateAccount, type AccountUpdatePayload,
} from '@/lib/api/accounts'
import { useDomainStore } from '@/stores/domain'
import type { Account } from '@/types/api'

const notificationMode = (account: Account): SignalNotificationMode =>
  account.execution_notification_status === 'none' ? 'off'
    : account.execution_notification_status === 'function' ? 'function' : 'feishu'

const notificationCode = (account: Account) => notificationMode(account) === 'function'
  ? account.execution_notification_code ?? SIGNAL_NOTIFICATION_EXAMPLE : SIGNAL_NOTIFICATION_EXAMPLE

export function SignalAccountNotificationPage({ id, account, onSaved }: { id: number; account: Account; onSaved: () => Promise<void> }) {
  const [baseline, setBaseline] = useState(account)
  const [mode, setMode] = useState(() => notificationMode(account))
  const [code, setCode] = useState(() => notificationCode(account))
  const [webhook, setWebhook] = useState('')
  const saveState = usePythonSave()
  const { saving, error, clearError } = saveState
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const savedMode = notificationMode(baseline)
  const key = extractFeishuKey(webhook)
  const dirty = mode !== savedMode || (mode === 'feishu' ? !!key : mode === 'function' && code !== notificationCode(baseline))
  const blocked = (mode === 'feishu' && !key && !baseline.feishu_configured) || (mode === 'function' && !code.trim())
  const reset = (next: Account) => {
    setMode(notificationMode(next))
    setCode(notificationCode(next))
    setWebhook('')
    clearError()
  }
  const save = (testRunning: boolean) => saveState.save(dirty && !blocked && !testRunning, async () => {
    const current = await getAccount(id)
    assertPythonBaseline(current.execution_notification_code, baseline.execution_notification_code)
    let patch: AccountUpdatePayload
    if (mode === 'off') patch = { execution_notification_code: null, feishu_key: null }
    else if (mode === 'function') patch = { execution_notification_code: code, feishu_key: null }
    else {
      patch = key ? { feishu_key: key } : {}
      if (savedMode !== 'feishu') patch.execution_notification_code = await getDefaultAccountNotification(id)
    }
    const updated = await updateAccount(id, patch)
    setBaseline(updated)
    void onSaved()
    setWebhook((value) => value === webhook ? '' : value)
    void refreshAccounts()
  })
  return <section className="flex h-full min-h-0 flex-col px-4 py-5 md:px-5">
    <SignalNotificationSetup mode={mode} webhook={webhook} code={code} name={account.name} market={account.market}
      allowOff savedWebhook={baseline.feishu_configured} saving={saving}
      draftKey={draftKey('account_notification', `/accounts/${id}/edit/notification`)}
      savedCode={baseline.execution_notification_code ?? ''}
      testDraft={(method, draft) => method === 'feishu' ? testAccountFeishu(id, draft) : testAccountNotificationFunction(id, draft)}
      heading={<AccountPageTitle accountId={id} page="通知设置" name={account.name} channel={account.trade_channel} market={account.market} />}
      footer={(testRunning) => <EditSaveBar layout="inline" changes={dirty ? ['通知设置已改'] : []}
        blocked={blocked} saveDisabled={testRunning} saving={saving} error={error}
        onCancel={() => reset(baseline)} onSave={() => void save(testRunning)} />}
      onModeChange={(next) => { setMode(next); clearError() }}
      onWebhookChange={(next) => { setWebhook(next); clearError() }}
      onCodeChange={(next) => { setCode(next); clearError() }}
    />
  </section>
}
