import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider, useLocation } from 'react-router'
import { ControlWarningPanel } from '../src/features/account/ControlWarningPanel'
import { AccountControlSummary } from '../src/features/account/ControlMonitor'
import { ControlScopeUsage } from '../src/features/account/ControlUsage'
import { CenteredModal } from '../src/components/ui/CenteredModal'
import { useLiveControl, type ControlFrame } from '../src/stores/liveControl'
import { useDomainStore } from '../src/stores/domain'
import type { AccountDashboardItem } from '../src/types/api'
import '../src/styles/theme.css'

function frame(used: number, accountId = 1, overrides: Partial<ControlFrame> = {}): ControlFrame {
  return {
    account_id: accountId, execution_id: 'fixture', instance_id: 'fixture', seq: 1,
    sampled_at_ms: Date.now(), status: 'running', stale: false, incomplete: false, pending_policy: false,
    timezone: 'Asia/Shanghai', control_date: '2026-10-16',
    actual_policy: { operations: { cancel_order: { daily_warning_percent: 80 } } },
    saved_policy: { operations: { cancel_order: { daily_warning_percent: 90 } } },
    rules: [{ id: 'account:cancel_order:per_day', operation: 'cancel_order', scope: 'account', symbol: null,
      rule: 'per_day', value: used, limit: 100, remaining: Math.max(0, 100 - used), estimated_wait_ms: 0, on_trigger: 'block' }],
    metrics: [], waiting_total: 0, waiting: [], records: [], ...overrides,
  }
}

function publish(data: ControlFrame[]) {
  const version = useLiveControl.getState().version + 1
  useLiveControl.getState().apply({ stream_id: 'warnings-fixture', version, kind: 'snapshot', data })
}

useDomainStore.setState({ accounts: [1, 2].map(account_id => ({
  account_id, name: account_id === 1 ? '趋势交易账户' : '套利交易账户', remark: null, market: 'futures',
  trade_channel: 'ctp', is_started: true, portfolio_id: null, is_scheduled: false, next_run_time: null,
  total_asset: 0, currency: 'CNY', holdings_count: 0, position_weights: [], last_is_success: null, last_exec_at: null,
} satisfies AccountDashboardItem)) })
publish([frame(Number(new URLSearchParams(location.search).get('used') ?? 79))])

export function Fixture() {
  const current = useLiveControl(state => state.frames[1])
  const location = useLocation()
  const [modalOpen, setModalOpen] = useState(false)
  return <main className="min-h-screen bg-bg p-6 text-ink-1">
    <h1 className="text-lg font-semibold">流控预警测试</h1>
    <p className="mt-2 text-sm text-ink-3">合成快照，不发送交易或通知</p>
    <label className="mt-4 block text-sm">操作输入<input aria-label="操作输入" className="ml-2 border border-line bg-surface p-2" /></label>
    <div className="my-4 flex flex-wrap gap-3 text-sm">
      <button onClick={() => publish([frame(80)])}>达到预警线</button>
      <button onClick={() => publish([frame(85)])}>用量增加</button>
      <button onClick={() => publish([frame(100)])}>额度用尽</button>
      <button onClick={() => publish([frame(99, 1, { stale: true })])}>数据陈旧</button>
      <button onClick={() => publish([frame(70)])}>恢复正常</button>
      <button onClick={() => publish([frame(80), frame(100, 2)])}>多账户预警</button>
      <button onClick={() => publish([frame(80, 1, { control_date: '2026-10-17' })])}>新统计日</button>
      <button onClick={() => setModalOpen(true)}>打开确认弹窗</button>
    </div>
    <section aria-label="账户摘要" className="max-w-lg rounded-xl border border-line bg-surface p-4">
      <h2 className="mb-3 text-sm font-medium">趋势交易账户</h2>
      <AccountControlSummary accountId={1} />
      <ControlScopeUsage frame={current} operation="cancel_order" />
    </section>
    <p className="mt-4 text-xs" data-route>{location.pathname}</p>
    <CenteredModal open={modalOpen} title="确认操作" onClose={() => setModalOpen(false)}><p>验证预警浮窗位于确认弹窗下方。</p></CenteredModal>
    <ControlWarningPanel />
  </main>
}

const router = createBrowserRouter([{ path: '*', element: <Fixture /> }])
createRoot(document.getElementById('root')!).render(<RouterProvider router={router} />)
