import { usePolling } from '@/lib/hooks/usePolling'
import { useEffect, useRef, useState } from 'react'
import { BackLink } from '@/components/ui/BackLink'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Segmented } from '@/components/ui/Segmented'
import { usePythonRun } from '@/components/ui/pythonWorkbenchState'
import { EditLoading } from '@/features/account/editUi'
import { NotificationWorkbench } from '@/features/notification/NotificationWorkbench'
import { SystemAlertFields } from '@/features/system/SystemAlertFields'
import { useSystemAlert } from '@/features/system/useSystemAlert'
import { systemAlertKey, systemAlertLabel } from '@/features/system/systemAlertDraft'
import { getDefaultSystemNotification, testSystemNotificationFunction, type SystemNotificationEvent, type SystemNotificationTestResult } from '@/lib/api/init'

export function SystemEditNotificationPage() {
  const initialized = useRef(false)
  const state = useSystemAlert()
  const { draft, baseline, loading, set } = state
  const template = usePolling(getDefaultSystemNotification, { queryKey: 'system-notification-template', intervalMs: 0 })
  const defaultCode = template.data
  const templateError = template.error
  const loadTemplate = template.refresh
  const [sample, setSample] = useState<SystemNotificationEvent>('execution_error')
  // 模板只是初始草稿；进入工作台不会启用或保存通知。
  useEffect(() => {
    if (!initialized.current && !loading && draft && baseline && defaultCode !== null) {
      initialized.current = true
      if (!draft.code.trim() && draft.code === baseline.code) set({ code: defaultCode })
    }
  }, [loading, draft, baseline, defaultCode, set])
  const key = draft ? systemAlertKey(draft) : null
  const trial = usePythonRun<SystemNotificationTestResult>({
    code: draft?.code ?? '', contextKey: JSON.stringify([key, sample, draft?.mode, state.configured]), enabled: !state.saveState.saving && !loading,
    execute: (code) => testSystemNotificationFunction(code, key, sample),
    failed: (cause) => ({ ok: false, message: cause instanceof Error ? cause.message : String(cause) }),
    toEditorResult: (result) => ({ valid: result.ok, errorMessage: result.ok ? null : result.message, errorLine: result.error_line, traceback: result.traceback }),
  })
  if (state.loadError && !draft) return <ErrorNotice intent="read" title="系统告警配置读取失败" error={state.loadError} onRetry={() => void state.reload()} />
  if (templateError && defaultCode === null) return <ErrorNotice intent="read" title="系统告警模板读取失败" error={templateError} onRetry={() => void loadTemplate()} />
  if (!draft || !baseline || defaultCode === null) return <EditLoading />
  const busy = state.saveState.saving || trial.running
  const changeCode = (code: string) => { state.set({ code }); state.saveState.clearError() }
  return (
    <NotificationWorkbench
      kind="system_notification" storageKey="axon.systemNotificationWorkbench"
      header={<>
        <ErrorNotice intent="read" hasData title="系统告警配置读取失败" error={state.loadError} onRetry={state.reload} />
        <ErrorNotice intent="read" hasData title="系统告警模板读取失败" error={templateError} onRetry={loadTemplate} />
        <div className="flex items-center gap-3">
          <BackLink to="/settings" label="返回系统告警" />
          <h1 className="text-[18px] font-semibold">系统告警函数</h1>
        </div>
        <p className="mt-2 text-[13px] text-ink-3">{systemAlertLabel(baseline, state.configured)}{state.dirty ? ' · 当前草稿未保存' : ''}</p>
      </>}
      controls={<div className="mt-4 space-y-4">
        <SystemAlertFields compact draft={draft} configured={state.configured} onChange={(patch) => { state.set(patch); state.saveState.clearError() }} disabled={busy} />
        <div><p className="mb-2 text-[13px] text-ink-2">试跑样例</p><Segmented size="sm" value={sample} options={[{ value: 'execution_error', label: '执行异常', disabled: busy }, { value: 'execution_timeout', label: '执行超时', disabled: busy }]} onChange={setSample} /></div>
        <p className="text-[12px] text-ink-3">试跑当前函数草稿，不保存配置；默认模式下保存的函数仅留存备用。要启用函数，请选择自定义。</p>
      </div>}
      runDisabled={loading} code={draft.code} onChange={changeCode} defaultCode={defaultCode} onReset={() => changeCode(defaultCode)}
      running={trial.running} result={trial.editorResult} message={trial.result?.message} stale={trial.stale}
      onRun={() => { state.saveState.clearError(); void trial.run() }}
      saveAction={{ dirty: state.dirty, saving: state.saveState.saving, error: state.saveState.error, disabled: trial.running || state.loading, blocked: state.blocked, blockedReason: state.blocked ? '自定义模式需填写函数' : undefined, onSave: () => void state.save(!trial.running), onRestore: () => { state.restore(); state.saveState.clearError() } }}
      docHref="/docs/system-notification"
    />
  )
}
