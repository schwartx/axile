import { useId, useState } from 'react'
import { Check, ExternalLink } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Link } from '@/components/ui/nav'
import { getDefaultAccountNotification, testAccountFeishu, type AccountFeishuTestResult, type AccountUpdatePayload } from '@/lib/api/accounts'
import type { Account } from '@/types/api'
import { AccountConfigDialog } from './AccountConfigDialog'
import { FeishuWebhookInput } from './FeishuWebhookInput'
import { basicNotificationPatch, basicNotificationStatus } from './basicNotification'
import { TEXT } from './editUi'
import { extractFeishuKey } from './feishuUpdate'
import { NOTIFICATION_STATUS_LABEL } from './notificationStatus'

type SavedNotification = Pick<Account, 'execution_notification_code' | 'execution_notification_status' | 'feishu_configured'>

export function AccountBasicDialog({ accountId, name, remark, notification, onClose, onSaved }: {
  accountId: number
  name: string
  remark?: string | null
  notification?: SavedNotification
  onClose: () => void
  onSaved: (updated: Account) => void
}) {
  const nameErrorId = useId()
  const [baseline] = useState(() => ({ name, remark: remark ?? '', notification }))
  const [draftName, setName] = useState(baseline.name)
  const [draftRemark, setRemark] = useState(baseline.remark)
  const savedNotification = baseline.notification
  const savedCode = savedNotification?.execution_notification_code ?? null
  const [code, setCode] = useState(savedCode)
  const [webhook, setWebhook] = useState('')
  const [clearWebhook, setClearWebhook] = useState(false)
  const [defaultCode, setDefaultCode] = useState<string | null>(null)
  const [resetting, setResetting] = useState(false)
  const [templateError, setTemplateError] = useState<unknown>(null)
  const [test, setTest] = useState<AccountFeishuTestResult | 'busy' | null>(null)
  const trimmedName = draftName.trim()
  const key = extractFeishuKey(webhook)
  const status = savedNotification && basicNotificationStatus(savedNotification, code, clearWebhook, defaultCode)
  const patch = trimmedName && !resetting ? {
    ...(trimmedName !== baseline.name ? { name: trimmedName } : {}),
    ...(draftRemark !== baseline.remark ? { remark: draftRemark || null } : {}),
    ...(savedNotification ? basicNotificationPatch(savedCode, code, webhook, clearWebhook, defaultCode) : {}),
  } : null

  const preparePatch = async (current: AccountUpdatePayload) => {
    if (!current.feishu_key || code?.trim() || defaultCode !== null) return current
    return { ...current, execution_notification_code: await getDefaultAccountNotification(accountId) }
  }
  const resetNotification = async () => {
    setResetting(true)
    setTemplateError(null)
    try {
      const template = await getDefaultAccountNotification(accountId)
      setDefaultCode(template)
      setCode(template)
    } catch (error) {
      setTemplateError(error)
    } finally {
      setResetting(false)
    }
  }
  const testWebhook = async () => {
    setTest('busy')
    try {
      setTest(await testAccountFeishu(accountId, key))
    } catch (error) {
      setTest({ ok: false, message: error instanceof Error ? error.message : String(error) })
    }
  }

  return <AccountConfigDialog accountId={accountId} accountName={baseline.name} title="基本信息"
    patch={patch} preparePatch={preparePatch} onClose={onClose} onSaved={onSaved}>
    <label className="grid gap-1 text-sm text-ink-2">
      名称
      <input className={TEXT} value={draftName} required aria-invalid={!trimmedName}
        aria-describedby={trimmedName ? undefined : nameErrorId} onChange={event => setName(event.target.value)} />
    </label>
    {!trimmedName && <p id={nameErrorId} className="text-xs text-warn">名称不能为空</p>}
    <label className="grid gap-1 text-sm text-ink-2">
      备注
      <input className={TEXT} value={draftRemark} placeholder="可选" onChange={event => setRemark(event.target.value)} />
    </label>
    {savedNotification && status && <section className="space-y-2 border-t border-line pt-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
        <h3 className="text-ink-2">执行通知</h3>
        <span>{NOTIFICATION_STATUS_LABEL[status]}</span>
        <Link className="inline-flex items-center gap-1 text-accent hover:underline" to={`/accounts/${accountId}/edit/notification`}>
          {status === 'function' ? '编辑与测试' : '自定义通知函数'} <ExternalLink size={13} aria-hidden />
        </Link>
        {status === 'function' && <button type="button" disabled={resetting}
          className="cursor-pointer text-accent hover:underline disabled:opacity-45" onClick={() => void resetNotification()}>
          {resetting ? '读取中…' : '重置为默认'}
        </button>}
      </div>
      <ErrorNotice intent="mutation" title="默认模板读取失败" error={templateError} variant="compact" onRetry={resetNotification} />
      {status !== 'function' && <>
        <FeishuWebhookInput value={webhook} configured={savedNotification.feishu_configured} onChange={value => {
          setWebhook(value)
          setClearWebhook(false)
          setTest(null)
        }} />
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {savedNotification.feishu_configured && <button type="button" className="cursor-pointer text-ink-2 hover:underline"
            onClick={() => { setClearWebhook(!clearWebhook); setWebhook(''); setTest(null) }}>
            {clearWebhook ? '撤销清除' : '清除 Webhook'}
          </button>}
          {clearWebhook && <span className="text-xs text-ink-3">保存后关闭通知</span>}
          <button type="button" className="cursor-pointer text-accent enabled:hover:underline disabled:opacity-45"
            disabled={(!key && !savedNotification.feishu_configured) || clearWebhook || test === 'busy'}
            onClick={() => void testWebhook()}>{test === 'busy' ? '测试中…' : '测试推送'}</button>
        </div>
        {test && test !== 'busy' && <p role="status" className={`flex items-center gap-1.5 text-[13px] ${test.ok ? 'text-accent' : 'text-warn'}`}>
          {test.ok && <Check size={14} aria-hidden />}{test.message}
        </p>}
      </>}
    </section>}
  </AccountConfigDialog>
}
