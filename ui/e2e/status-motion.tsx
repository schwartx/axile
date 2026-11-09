import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { ServiceConnectionStatus } from '../src/components/ServiceConnectionStatus'
import { EditSaveBar } from '../src/features/account/editUi'
import { RuntimeSyncStatus } from '../src/features/account/RuntimeSyncStatus'
import { AccountControlSummary } from '../src/features/account/ControlMonitor'
import { ControlScopeUsage } from '../src/features/account/ControlUsage'
import { ControlWaitingSection } from '../src/features/account/ControlActivity'
import { TimeCheckResult } from '../src/features/system/timeCheck/TimeCheckResult'
import { useDomainStore } from '../src/stores/domain'
import { useLiveConnection } from '../src/stores/liveConnection'
import { useLiveControl, type ControlFrame } from '../src/stores/liveControl'
import type { TimeCheckSnapshot } from '../src/lib/api/timeCheck'
import type { AccountRuntimeSync } from '../src/types/api'
import '../src/styles/theme.css'

useDomainStore.setState({ accounts: [], accountsUpdatedAt: Date.now(), accountsError: null })
useLiveConnection.setState({ connected: true })
const initial: ControlFrame = {
  account_id: 1, execution_id: 'synthetic', instance_id: 'synthetic', status: 'running', seq: 1,
  sampled_at_ms: Date.now(), stale: false, incomplete: false, pending_policy: false,
  timezone: 'Asia/Shanghai', control_date: '2026-11-09', actual_policy: null, saved_policy: null,
  rules: [{ id: 'account:cancel_order:per_day', operation: 'cancel_order', scope: 'account', symbol: null,
    rule: 'per_day', value: 20, limit: 100, remaining: 80, estimated_wait_ms: 0, on_trigger: 'block' }],
  metrics: [], waiting_total: 2,
  waiting: [{ id: 1, operation: 'cancel_order', symbol: 'TEST', priority: 10, waited_ms: 90125,
    estimated_wait_ms: 3600100, kind: 'quota', hits: [{ scope_type: 'account' }] }], records: [],
}
useLiveControl.setState({ frames: { 1: initial }, connected: true })
const settings: TimeCheckSnapshot['settings'] = { version: 1, source: 'aliyun', custom_host: '',
  custom_port: 123, threshold_ms: 500, enabled: false, cron_expr: '0 * * * *' }

function Fixture() {
  const [step, setStep] = useState(0)
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(true)
  const [checking, setChecking] = useState(false)
  const frame = useLiveControl(state => state.frames[1])
  const state: AccountRuntimeSync = { status: step ? 'synchronized' : 'pending', revision: 1,
    attempts: step + 1, last_error: null, last_attempt_at: null, synchronized_at: null }
  const runtime = { data: state, queried: true, loading: false, error: null, retryError: null,
    invalidate() {}, acceptSaved() {}, refresh: async () => {}, retry: async () => {} }
  const snapshot: TimeCheckSnapshot = { ready: true, settings, status: 'disabled', next_run_at: null,
    state: { running: checking, result: { finished_at: `2026-11-09T10:00:0${step}`,
      trigger: 'manual', host: 'synthetic.invalid', port: 123, settings,
      ntp: { status: 'ok', offset_ms: step ? -12.34 : 1.25, delay_ms: 0.12, samples: 3 } } } }
  const advance = () => {
    setStep(value => value + 1)
    useLiveControl.setState({ frames: { 1: { ...initial, rules: [{ ...initial.rules[0], value: 21, remaining: 79 }], waiting_total: 3 } } })
  }
  return <main className="min-h-dvh space-y-5 bg-bg p-6 pb-32 text-ink-1">
    <h1 className="text-lg font-semibold">状态动效验收</h1>
    <p>所有按钮仅修改合成状态，不访问柜台或发送通知。</p>
    <div className="flex flex-wrap gap-4">
      <button onClick={advance}>更新合成状态</button>
      <button onClick={() => useDomainStore.setState({ accountsUpdatedAt: Date.now() - 10_000 })}>更新新鲜度</button>
      <button onClick={() => useDomainStore.setState({ accountsError: new TypeError('Failed to fetch') })}>模拟连接故障</button>
      <button onClick={() => useDomainStore.setState({ accountsError: null })}>恢复连接</button>
      <button onClick={() => useLiveControl.setState({ frames: { 1: { ...frame, stale: true } } })}>保留陈旧快照</button>
      <button onClick={() => useLiveControl.setState({ frames: { 1: { ...frame, status: 'unknown', rules: [{ ...frame.rules[0], value: null }] } } })}>用量未知</button>
      <button onClick={() => setChecking(value => !value)}>切换检查中</button>
      <button onClick={() => { setSaving(false); setDirty(false) }}>完成保存</button>
    </div>
    <section aria-label="服务连接"><ServiceConnectionStatus /></section>
    <section aria-label="运行态同步"><RuntimeSyncStatus accountId={1} runtime={runtime} /></section>
    <section aria-label="流控摘要" className="max-w-xl space-y-3">
      <AccountControlSummary accountId={1} />
      <ControlScopeUsage frame={frame} operation="cancel_order" />
      <ControlWaitingSection frame={frame} operationNames={{ cancel_order: '撤单' }} />
    </section>
    <div className="max-w-md"><TimeCheckResult snapshot={snapshot} /></div>
    <EditSaveBar changes={dirty ? ['合成参数已修改'] : []} saving={saving} onCancel={() => setDirty(false)} onSave={() => setSaving(true)} />
  </main>
}

const router = createMemoryRouter([{ path: '/', element: <Fixture /> }])
createRoot(document.getElementById('root')!).render(<RouterProvider router={router} />)
