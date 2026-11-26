import { useState, type ReactNode } from 'react'
import { Card } from '@/components/ui/Card'
import { ChoiceGroup } from '@/components/ui/ChoiceGroup'
import { PythonWorkbenchPane } from '@/components/ui/PythonWorkbenchPane'
import { checksAreEmpty, type PythonChecks } from '@/components/ui/pythonChecks'
import { Row } from '@/features/account/editUi'
import { FeishuWebhookInput } from '@/features/account/FeishuWebhookInput'
import { SignalSetupColumns } from './SignalSetupColumns'

export type SignalNotificationMode = 'feishu' | 'function'

export function SignalNotificationSetup({ mode, webhook, code, heading, footer, onModeChange, onWebhookChange, onCodeChange }: {
  mode: SignalNotificationMode
  webhook: string
  code: string
  heading: ReactNode
  footer: ReactNode
  onModeChange: (mode: SignalNotificationMode) => void
  onWebhookChange: (webhook: string) => void
  onCodeChange: (code: string) => void
}) {
  const [editorStatus, setEditorStatus] = useState<HTMLDivElement | null>(null)
  const [checks, setChecks] = useState<PythonChecks | null>(null)
  const custom = mode === 'function'
  const currentChecks = checks?.document === code ? checks : null
  const diagnosticCount = currentChecks ? currentChecks.lsp.diagnostics.length + currentChecks.contract.diagnostics.length : 0
  const checksUnavailable = currentChecks?.lsp.status === 'failed' || currentChecks?.contract.status === 'failed'
  const codeStatus = !code.trim() ? '待填写'
    : diagnosticCount ? `${diagnosticCount} 条诊断`
    : checksUnavailable ? '检查暂不可用'
    : checksAreEmpty(currentChecks) ? '代码检查通过' : '检查中…'
  const sidebar = <aside aria-label="录入检查">
    <Card className="p-3.5">
      <h2 className="text-[15px] font-semibold text-ink-1">录入检查</h2>
      <dl className="mt-3 flex flex-wrap justify-between gap-x-3 gap-y-1 border-t border-line pt-3 text-[13px]">
        <dt className="text-ink-2">{custom ? '通知函数' : '飞书 Webhook'}</dt>
        <dd className={custom && code.trim() && (diagnosticCount > 0 || checksUnavailable) ? 'text-warn' : undefined}>
          <span role="status">{custom ? codeStatus : webhook.trim() ? '已填写' : '待填写'}</span>
        </dd>
      </dl>
      {custom && <p className="mt-3 text-[13px] text-ink-2">通知渠道和凭据由函数配置。</p>}
    </Card>
  </aside>

  return <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
    <div className="flex shrink-0 flex-wrap items-start justify-between gap-4">
      {heading}
      <ChoiceGroup variant="compact" ariaLabel="通知方式" value={mode} onChange={(next) => {
        if (next === mode) return
        setChecks(null)
        onModeChange(next)
      }} options={[
        { value: 'feishu', label: '飞书' },
        { value: 'function', label: '自定义函数' },
      ]} />
    </div>
    <SignalSetupColumns sidebar={sidebar} footer={footer}>
      {mode === 'feishu' && <Row label="飞书 Webhook">
        <FeishuWebhookInput value={webhook} required onChange={onWebhookChange} />
      </Row>}
      {custom && <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_auto] overflow-hidden rounded-[8px] border border-line">
        <PythonWorkbenchPane kind="account_notification" title="通知函数" code={code} savedCode=""
          draftKey="signal:setup:notify" storageKey="axile.signalNotification.editorSplit" docHref="/docs/notification"
          onChange={onCodeChange} onChecks={setChecks} editorRef={null} statusTarget={editorStatus} compactStatus={false}
          running={false} stale={false} result={null} skipEmptyChecks />
        <div ref={setEditorStatus} />
      </div>}
    </SignalSetupColumns>
  </div>
}
