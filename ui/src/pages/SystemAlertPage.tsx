import { ExternalLink } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { usePythonRun } from '@/components/ui/pythonWorkbenchState'
import { EditLoading, EditSaveBar, Section } from '@/features/account/editUi'
import { SystemAlertFields } from '@/features/system/SystemAlertFields'
import { SystemNotificationResult } from '@/features/system/SystemNotificationResult'
import { useSystemAlert } from '@/features/system/useSystemAlert'
import { systemAlertKey, systemAlertLabel } from '@/features/system/systemAlertDraft'
import { testFeishu, type TestResult } from '@/lib/api/init'

export function SystemAlertPage() {
  const state = useSystemAlert()
  const { draft, baseline } = state
  const key = draft ? systemAlertKey(draft) : null
  const test = usePythonRun<TestResult>({
    code: 'default', contextKey: JSON.stringify([key, draft?.mode, state.configured]),
    enabled: !state.saveState.saving && draft?.mode === 'default' && Boolean(key ?? state.configured),
    execute: () => testFeishu(key), failed: (cause) => ({ ok: false, message: String(cause) }),
    toEditorResult: (result) => ({ valid: result.ok, errorMessage: result.ok ? null : result.message }),
  })
  if (state.loadError && !draft) return <ErrorNotice intent="read" title="系统告警配置读取失败" error={state.loadError} onRetry={() => void state.reload()} />
  if (!draft || !baseline) return <EditLoading />
  return (
    <section>
      <ErrorNotice intent="read" hasData title="系统告警配置读取失败" error={state.loadError} onRetry={state.reload} />
      <h1 className="text-[22px] font-semibold text-ink-1">系统告警</h1>
      <p className="mt-2 text-[14px] text-ink-2">账户执行异常或总超时时发送。保存后立即生效。</p>
      <p className="mt-2 text-[13px] text-ink-3"><InkRewrite text={systemAlertLabel(baseline, state.configured)} tone="label" />{state.dirty ? ' · 当前草稿未保存' : ''}</p>
      <Section label="通知配置">
        <div className="md:col-span-2">
          <SystemAlertFields showModeSelector={false} draft={draft} configured={state.configured} onChange={(patch) => { state.set(patch); state.saveState.clearError() }} disabled={state.saveState.saving || test.running} keyActions={draft.mode === 'default' ? <button type="button" className="cursor-pointer rounded-[9px] border border-line px-4 py-2 text-[14px] disabled:opacity-45" disabled={!test.canRun || state.loading} onClick={() => void test.run()}><InkRewrite text={test.running ? '测试中…' : '测试推送'} tone="label" /></button> : undefined} />
          {test.result && <p role="status" className={`mt-3 text-[13px] ${test.result.ok ? 'text-ink-2' : 'text-warn'}`}>{test.stale ? '上次测试结果 · ' : ''}{test.result.message}</p>}
          <Link to="/settings/notification" className="mt-4 inline-flex items-center gap-1 text-[13px] text-ink-3 transition-colors hover:text-accent hover:underline">高级设置 · 自定义执行通知函数 <ExternalLink size={13} aria-hidden /></Link>
        </div>
      </Section>
      <Section label="最近一次真实系统告警"><div className="md:col-span-2"><SystemNotificationResult /></div></Section>
      <EditSaveBar changes={state.dirty ? ['系统告警配置'] : []} blocked={state.blocked || test.running || state.loading} onCancel={() => { state.restore(); state.saveState.clearError() }} onSave={() => void state.save(!test.running)} saving={state.saveState.saving} error={state.saveState.error} />
    </section>
  )
}
