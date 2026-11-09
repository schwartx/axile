import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { AcctPortfolio } from '../src/pages/setup/AccountSteps'
import { AccountEditPage } from '../src/pages/AccountEditPage'
import { useDomainStore } from '../src/stores/domain'
import { useWizardStore } from '../src/stores/wizard'
import { useChannelCatalogStore } from '../src/stores/channels'
import '../src/styles/theme.css'

useDomainStore.setState({ portfolios: [
  { id: 1, name: '商品趋势', market: '期货', description: '覆盖国内商品期货的趋势组合', custom_calc_py_code: 'def calculate_portfolio(context):\n    return {"SHFE.rb2610": 0.2, "SHFE.au2612": -0.15}\n', status: null, tag: null, updated_at: '2026-10-13', created_at: '2026-10-13' },
  { id: 2, name: '备选组合', market: '期货', description: null, custom_calc_py_code: Array.from({ length: 100 }, (_, index) => `# 策略说明 ${index}`).join('\n') + '\ndef calculate_portfolio(context):\n    return {}', status: null, tag: null, updated_at: '2026-10-13', created_at: '2026-10-13' },
  { id: 3, name: '空仓组合', market: '期货', description: '暂无交易信号时保持空仓', custom_calc_py_code: 'def calculate_portfolio(context):\n    return {}', status: null, tag: null, updated_at: '2026-10-13', created_at: '2026-10-13' },
] })
useWizardStore.getState().setAcct({ portfolioId: 1 })
useChannelCatalogStore.setState({ channels: [], loading: false })
const router = createMemoryRouter([
  { path: '/', element: <AcctPortfolio /> },
  { path: '/accounts/:id/edit/portfolio', element: <main className="p-5"><AccountEditPage section="portfolio" /></main> },
], { initialEntries: [new URLSearchParams(location.search).has('edit') ? '/accounts/1/edit/portfolio' : '/'] })
document.documentElement.classList.add('dark')
createRoot(document.getElementById('root')!).render(<main className="h-dvh bg-bg"><RouterProvider router={router} /></main>)
