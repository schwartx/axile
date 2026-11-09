import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { DashboardPage } from '../src/pages/DashboardPage'
import { AccountEditGuard } from '../src/features/account/AccountEditGuard'
import { AccountDetailPage } from '../src/pages/AccountDetailPage'
import { PortfoliosPage } from '../src/pages/PortfoliosPage'
import { PortfolioEditPage } from '../src/pages/PortfolioEditPage'
import { useDomainStore } from '../src/stores/domain'
import { useChannelCatalogStore } from '../src/stores/channels'
import '../src/styles/theme.css'

useDomainStore.setState({ accounts: [], portfolios: [{
  id: 1, name: '商品趋势', market: '期货', description: '保留历史的组合',
  custom_calc_py_code: 'def calculate_portfolio(context):\n    return {"rb2610": 1.0}\n',
  status: null, tag: null, updated_at: '2026-11-09', created_at: '2026-11-09',
}] })
useChannelCatalogStore.setState({ channels: [], loading: false })
const router = createMemoryRouter([
  { path: '/', element: <DashboardPage /> },
  { path: '/accounts/:id', element: <AccountDetailPage /> },
  { path: '/accounts/:id/edit', element: <AccountEditGuard />, children: [{ index: true, element: <p>不可显示的编辑器</p> }] },
  { path: '/portfolios', element: <PortfoliosPage /> },
  { path: '/portfolios/:id/edit', element: <PortfolioEditPage /> },
], { initialEntries: [new URLSearchParams(location.search).has('account') ? (new URLSearchParams(location.search).has('edit') ? '/accounts/1/edit' : '/accounts/1') : '/portfolios'] })
createRoot(document.getElementById('root')!).render(<main className="min-h-dvh bg-bg p-5 text-ink-1"><RouterProvider router={router} /></main>)
