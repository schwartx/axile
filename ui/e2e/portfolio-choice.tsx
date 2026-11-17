import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { AcctConfirm, AcctPortfolio } from '../src/pages/setup/AccountSteps'
import { WizardNav } from '../src/features/setup/WizardNav'
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
const params = new URLSearchParams(location.search)
if (params.has('empty')) useDomainStore.setState({ portfolios: [] })
useWizardStore.getState().setAcct({
  name: '新建测试账户', channel: 'ctp', portfolioId: params.has('unbound') || params.has('empty') ? null : 1,
  algorithm: { method: 'SINGLE-MAKER', params: {} }, timerTab: 'custom', rawCron: '0 9 * * 1-5',
})
useChannelCatalogStore.setState({ channels: [{
  channel: 'ctp', label: 'CTP', description: '', icon: 'landmark', available: true,
  missing_packages: [], install_extra: null, calendar: null, market: '期货', currency: 'CNY',
  portfolio: { market_label: '期货', example_symbols: [] },
  account_form: { fields: [], notices: [] }, schedule: { kind: 'cn_futures', night: null },
  units: { quantity_kind: 'contract', quantity_label: '手', quantity_max_decimals: 0, price_label: '价格', notional_label: '名义价值' },
  ui: { account_connect_lead: '', leverage_title: '杠杆', leverage_note: '', long_leverage_label: '做多杠杆', short_leverage_label: '做空杠杆', show_short_leverage: true, position_value_label: '持仓价值' },
  defaults: {
    account_control_preset: 'default', long_leverage: 1, short_leverage: 1, execution_timeout: 180,
    trade_algorithm: { method: 'SINGLE-MAKER', params: {} }, empty_positions_algorithm: { method: 'SINGLE-MAKER', params: {} },
  },
  leverage: { min: 0, max: 10, step: 0.1 },
}], loading: false })
const router = createMemoryRouter([
  { path: '/', element: <AcctPortfolio /> },
  { path: '/setup/acct/portfolio', element: <AcctPortfolio /> },
  { path: '/setup/acct/trade', element: <WizardNav prevTo="/setup/acct/portfolio" nextTo="/setup/acct/confirm" /> },
  { path: '/setup/acct/confirm', element: <AcctConfirm /> },
  { path: '/accounts/:id', element: <p>账户详情</p> },
  { path: '/accounts/:id/edit/portfolio', element: <main className="p-5"><AccountEditPage section="portfolio" /></main> },
], { initialEntries: [params.has('edit') ? '/accounts/1/edit/portfolio' : '/'] })
document.documentElement.classList.add('dark')
createRoot(document.getElementById('root')!).render(<main className="h-dvh bg-bg"><RouterProvider router={router} /></main>)
