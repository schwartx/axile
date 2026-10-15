import { useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { PythonWorkbenchPane } from '../src/components/ui/PythonWorkbenchPane'
import type { PythonEditorHandle } from '../src/components/ui/PythonFunctionEditor'
import { ErrorNotice } from '../src/components/ui/ErrorNotice'
import { CenteredModal } from '../src/components/ui/CenteredModal'
import { TargetSnapshotControl } from '../src/features/portfolio/TargetSnapshotControl'
import { ExecutionErrorEvidence } from '../src/features/history/ExecutionErrorEvidence'
import { useExecutionRunner } from '../src/features/account/useExecutionRunner'
import { AccountDetail } from '../src/features/account/AccountDetail'
import { AccountHoldingsPage } from '../src/pages/AccountHoldingsPage'
import { PortfolioEditPage } from '../src/pages/PortfolioEditPage'
import { SystemConfigPage } from '../src/pages/SystemConfigPage'
import { AccountConnectionStatus } from '../src/features/account/AccountConnectionStatus'
import { ApiError } from '../src/lib/api/client'
import { useDomainStore } from '../src/stores/domain'
import type { AccountDashboardItem } from '../src/types/api'
import '../src/styles/theme.css'

const raw = (name: string) => `${name}开始\n${'synthetic error '.repeat(100)}\nTraceback\n  script.py:123\npassword=synthetic token=synthetic api_key=synthetic\n尾部根因-${name}`
const failure = (name: string) => new ApiError(410, raw(name), { code: 'SNAPSHOT_EXPIRED', requestId: 'req-97', rawText: raw(name), body: { detail: { message: raw(name), traceback: raw(name) } } })
const item: AccountDashboardItem = { account_id: 7, name: '合成账户 A', remark: null, market: 'demo', trade_channel: 'paper', is_started: false,
  portfolio_id: 9, is_scheduled: false, next_run_time: null, total_asset: 100, currency: 'CNY', holdings_count: 0, position_weights: [],
  last_is_success: null, last_exec_at: null, last_output_status: null, off_symbol_count: null }
useDomainStore.setState({ accounts: [item, { ...item, account_id: 8, name: '合成账户 B' }], portfolios: [{ id: 9, name: '合成组合', market: 'demo', description: '', custom_calc_py_code: 'def calculate_portfolio(context):\n    return {}\n' }] })

export function Basic() {
  const [error, setError] = useState<Error>(failure('A'))
  const [context, setContext] = useState('A')
  const [old, setOld] = useState(true)
  return <main className="min-h-dvh bg-bg p-5 text-ink-1">
    <button type="button" onClick={() => setError(failure('B'))}>后台更新为 B</button>
    <button type="button" onClick={() => setContext('B')}>切换上下文</button>
    <ErrorNotice title="合成操作失败" error={error} contextKey={context} onRetry={() => setError(failure('重试'))} />
    <button type="button" onClick={() => setOld(value => !value)}>切换首次计算</button>
    {(['full', 'compact'] as const).map(variant => <section key={variant} aria-label={variant} className="my-4">
      <TargetSnapshotControl snapshot={old ? { weights: {}, calculated_at: '2026-10-14T10:00:00' } : null} recalculating={false} error={error} variant={variant} onRecalculate={() => setError(failure('重算'))} />
    </section>)}
  </main>
}
function Embedded() {
  const [error, setError] = useState<Error | null>(failure('A'))
  return <CenteredModal open title="已有业务弹窗" onClose={() => {}}>
    <button type="button" onClick={() => setError(null)}>后台查询恢复成功</button>
    <ErrorNotice title="弹窗内读取失败" error={error} />
  </CenteredModal>
}
export function Evidence() {
  const [id, setId] = useState('A')
  const [poll, setPoll] = useState(0)
  const legacy = new URLSearchParams(location.search).has('legacy')
  return <main className="p-5"><button type="button" onClick={() => setId('B')}>切换执行</button>
    <button type="button" onClick={() => setPoll(value => value + 1)}>后台轮询更新</button><output>{poll}</output>
    <ExecutionErrorEvidence accountId={7} executionId={legacy ? null : id} recordId={legacy ? 1 : null} />
  </main>
}
export function Runner() {
  const runner = useExecutionRunner(7)
  return <main className="p-5"><button type="button" disabled={runner.running} onClick={() => void runner.start('exec')}>立即执行</button>
    <output data-testid="runner">{JSON.stringify({ running: runner.running, executionId: runner.executionId, status: runner.lastStatus?.status })}</output>
    <ErrorNotice title="状态待确认" error={runner.queryError} onRetry={runner.resumeQuery} evidence={[{ label: 'execution_id', value: runner.executionId ?? '' }]} />
    <ErrorNotice title="执行请求失败" error={runner.triggerError} />
  </main>
}
export function PythonFixture() {
  const [code, setCode] = useState('value = 1\n')
  const editor = useRef<PythonEditorHandle>(null)
  const [status, setStatus] = useState<HTMLDivElement | null>(null)
  return <main className="grid h-[90vh] grid-rows-[minmax(0,1fr)_32px] p-5">
    <PythonWorkbenchPane kind="portfolio" code={code} onChange={setCode} running={false} stale={false} result={null} onRun={() => {}}
      editorRef={editor} statusTarget={status} storageKey="test-97-python" title="合成代码" />
    <div ref={setStatus} />
  </main>
}
function AccountFixture() {
  const [current, setCurrent] = useState(item)
  return <>
    <button type="button" onClick={() => setCurrent({ ...item, account_id: 8, name: '合成账户 B' })}>切换账户</button>
    <AccountDetail accountId={current.account_id} item={current} />
  </>
}
function ConnectionFixture() {
  const [accountId, setAccountId] = useState(7)
  const [label, setLabel] = useState(new URLSearchParams(location.search).get('label') ?? '测试渠道的新故障文案')
  return <main className="p-5 text-ink-1">
    <button type="button" onClick={() => setAccountId(id => id === 7 ? 8 : 7)}>切换连接账户</button>
    <button type="button" onClick={() => setLabel('后台更新后的标签')}>后台更新标签</button>
    <div className="mt-5 w-[220px] max-w-full">
      <AccountConnectionStatus accountId={accountId} connection={{ status: 'error', observed_at: null, message: null, label }} />
    </div>
  </main>
}
const mode = new URLSearchParams(location.search).get('mode') ?? 'basic'
const element = mode === 'connection' ? <ConnectionFixture /> : mode === 'embedded' ? <Embedded /> : mode === 'python' ? <PythonFixture /> : mode === 'evidence' ? <Evidence /> : mode === 'runner' ? <Runner /> : mode === 'account' ? <AccountFixture /> : mode === 'holdings' ? <AccountHoldingsPage /> : mode === 'portfolio' ? <PortfolioEditPage /> : mode === 'system' ? <SystemConfigPage section="advanced" /> : <Basic />
const path = mode === 'portfolio' ? '/portfolios/9/edit' : mode === 'holdings' ? '/accounts/7/holdings' : '/accounts/7'
const pattern = mode === 'portfolio' ? '/portfolios/:id/edit' : mode === 'holdings' ? '/accounts/:id/holdings' : '/accounts/:id'
const router = createMemoryRouter([{ path: pattern, element }, { path: '*', element: <p>已离开测试上下文</p> }], { initialEntries: [path] })
createRoot(document.getElementById('root')!).render(<RouterProvider router={router} />)
