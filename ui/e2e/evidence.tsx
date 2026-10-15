import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { AccountExecutionsPage } from '../src/pages/AccountExecutionsPage'
import { ExecutionDetailPage } from '../src/pages/ExecutionDetailPage'
import { Toast } from '../src/components/Toast'
import { useDomainStore } from '../src/stores/domain'
import { useChannelCatalogStore } from '../src/stores/channels'
import type { AccountDashboardItem } from '../src/types/api'
import '../src/styles/theme.css'

const item: AccountDashboardItem = { account_id: 7, name: '合成账户 A', remark: null, market: 'demo', trade_channel: 'paper', is_started: false,
  portfolio_id: null, is_scheduled: false, next_run_time: null, total_asset: 100, currency: 'CNY', holdings_count: 0, position_weights: [],
  last_is_success: null, last_exec_at: null, last_output_status: null, off_symbol_count: null }
useDomainStore.setState({ accounts: [item, { ...item, account_id: 8, name: '合成账户 B' }], portfolios: [] })
useChannelCatalogStore.setState({ channels: [], loading: false })
const entry = new URLSearchParams(location.search).get('entry') ?? '/accounts/7/executions/A'
const router = createMemoryRouter([
  { path: '/accounts/:id/executions', element: <AccountExecutionsPage /> },
  { path: '/accounts/:id/executions/:executionId', element: <ExecutionDetailPage /> },
], { initialEntries: [entry] })
createRoot(document.getElementById('root')!).render(<main className="app-workspace min-h-dvh bg-bg p-5 text-ink-1">
  <button type="button" onClick={() => void router.navigate('/accounts/8/executions/B')}>切换测试账户</button>
  <RouterProvider router={router} /><Toast />
</main>)
