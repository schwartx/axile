import { Check } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { Card } from '@/components/ui/Card'
import { ChoiceGroup } from '@/components/ui/ChoiceGroup'
import { PythonWorkbenchPane } from '@/components/ui/PythonWorkbenchPane'
import { checksAreEmpty, type PythonChecks } from '@/components/ui/pythonChecks'
import { usePythonRun } from '@/components/ui/pythonWorkbenchState'
import { ValidationCardHeader } from '@/features/account/connectionValidation/ValidationCardHeader'
import { Row } from '@/features/account/editUi'
import { FeishuWebhookInput } from '@/features/account/FeishuWebhookInput'
import { extractFeishuKey } from '@/features/account/feishuUpdate'
import type { AccountFeishuTestResult } from '@/lib/api/accounts'
import { shortErrorReason } from '@/lib/errorInfo'
import { testSignalNotification } from './signalApi'
import { SignalSetupColumns } from './SignalSetupColumns'

export type SignalNotificationMode = 'feishu' | 'function' | 'off'

export function SignalNotificationSetup({ mode, webhook, code, name, market, heading, footer, onModeChange, allowOff = false, savedWebhook = false,
  saving = false, draftKey = 'signal:setup:notify', savedCode = '', testDraft, onWebhookChange, onCodeChange }: {
  mode: SignalNotificationMode
  webhook: string
  code: string
  name: string
  market: string
  heading: ReactNode
  footer: ReactNode
  allowOff?: boolean
  savedWebhook?: boolean
  saving?: boolean
  draftKey?: string
  savedCode?: string
  testDraft?: (mode: 'feishu' | 'function', draft: string) => Promise<AccountFeishuTestResult>
  onModeChange: (mode: SignalNotificationMode) => void
  onWebhookChange: (webhook: string) => void
  onCodeChange: (code: string) => void
}) {
  const [editorStatus, setEditorStatus] = useState<HTMLDivElement | null>(null)
  const [checks, setChecks] = useState<PythonChecks | null>(null)
  const [revision, setRevision] = useState(0)
  const custom = mode === 'function'
  const off = mode === 'off'
  const test = usePythonRun<AccountFeishuTestResult>({
    code: custom ? code : extractFeishuKey(webhook),
    contextKey: JSON.stringify([mode, name, market, revision]),
    enabled: Boolean(market) && !off && !saving,
    allowEmpty: mode === 'feishu' && savedWebhook,
    execute: (draft) => {
      const method = custom ? 'function' : 'feishu'
      return testDraft ? testDraft(method, draft) : testSignalNotification(method, draft, name, market)
    },
    failed: (cause) => ({ ok: false, message: shortErrorReason(cause) }),
    toEditorResult: (result) => ({
      valid: result.ok, errorMessage: result.ok ? null : result.message, errorLine: result.error_line, traceback: result.traceback,
    }),
  })
  const result = test.stale || test.running ? null : test.result
  const currentChecks = checks?.document === code ? checks : null
  const diagnosticCount = currentChecks ? currentChecks.lsp.diagnostics.length + currentChecks.contract.diagnostics.length : 0
  const checksUnavailable = currentChecks?.lsp.status === 'failed' || currentChecks?.contract.status === 'failed'
  const codeStatus = !code.trim() ? '待填写'
    : diagnosticCount ? `${diagnosticCount} 条诊断`
    : checksUnavailable ? '检查暂不可用'
    : checksAreEmpty(currentChecks) ? '代码检查通过' : '检查中…'
  const sidebar = !off && <aside aria-label="通知测试">
    <Card className="p-3.5">
      <ValidationCardHeader title="通知测试" status={custom ? codeStatus : ''}
        statusClassName={diagnosticCount > 0 || checksUnavailable ? 'text-warn' : undefined}
        action={test.running ? '测试中…' : custom ? '试跑通知' : '发送测试卡片'}
        disabled={!test.canRun} loading={test.running} onAction={() => void test.run()} />
      <div role="status" className="mt-3 border-t border-line pt-3 text-[13px] leading-5">
        {result ? result.ok ? (
          <p className="flex flex-wrap items-center gap-x-2 text-ink-2">
            <span className="inline-flex items-center gap-1.5 font-medium text-accent">
              <Check size={14} aria-hidden="true" />
              {custom ? '试跑完成' : '已发送'}
            </span>
            {!custom && <span>· 请到飞书群确认</span>}
          </p>
        ) : (
          <>
            <p className="font-medium text-warn">{custom ? '试跑失败' : '发送失败'}</p>
            <p className="mt-1 wrap-break-word text-ink-2">{result.message}</p>
          </>
        ) : (
          <p className="text-ink-2">{!market ? '请先在「账户与组合」选择市场。' : custom
            ? '用样例信号试跑，可能实际发送消息。'
            : '向飞书群发送一张样例调仓卡片。'}</p>
        )}
      </div>
    </Card>
  </aside>

  return <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
    <div className="flex shrink-0 flex-wrap items-start justify-between gap-4">
      {heading}
      <ChoiceGroup variant="compact" ariaLabel="通知方式" value={mode} onChange={(next) => {
        if (next === mode) return
        setChecks(null)
        setRevision((value) => value + 1)
        onModeChange(next)
      }} options={[
        { value: 'feishu', label: '飞书' },
        { value: 'function', label: '自定义函数' },
        ...(allowOff ? [{ value: 'off' as const, label: '关闭' }] : []),
      ]} />
    </div>
    <SignalSetupColumns sidebar={sidebar} footer={footer}>
      {off && <p className="text-[14px] text-ink-2">关闭推送后仍可执行调仓信号。</p>}
      {mode === 'feishu' && <Row label="飞书 Webhook">
        <FeishuWebhookInput value={webhook} configured={savedWebhook} required={!savedWebhook} onChange={(next) => {
          setRevision((value) => value + 1)
          onWebhookChange(next)
        }} />
      </Row>}
      {custom && <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_auto] overflow-hidden rounded-[8px] border border-line max-lg:min-h-72">
        <PythonWorkbenchPane kind="account_notification" title="通知函数" code={code} savedCode={savedCode}
          draftKey={draftKey} storageKey="axile.signalNotification.editorSplit" docHref="/docs/notification"
          onChange={(next) => {
            if (next === code) return
            setRevision((value) => value + 1)
            onCodeChange(next)
          }} onChecks={setChecks} editorRef={null} statusTarget={editorStatus} compactStatus={false}
          running={test.running} stale={false} result={result ? test.editorResult : null} skipEmptyChecks />
        <div ref={setEditorStatus} />
      </div>}
    </SignalSetupColumns>
  </div>
}
