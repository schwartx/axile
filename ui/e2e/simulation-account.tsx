import { createRoot } from 'react-dom/client'
import { PortfolioTrialAccounts } from '../src/features/portfolio/PortfolioTrialAccounts'
import { useCustomCalcValidation } from '../src/features/portfolio/useCustomCalcValidation'
import { useChannelCatalogStore } from '../src/stores/channels'
import { useDomainStore } from '../src/stores/domain'
import { simulationChannels } from './simulationAccountFixture'
import '../src/styles/theme.css'

useChannelCatalogStore.setState({ channels: simulationChannels, loading: false })
useDomainStore.setState({ accounts: [], accountsError: null })
export function Fixture() {
  const calc = useCustomCalcValidation('def calculate_portfolio(context):\n    return {"SHFE.rb2701": context.account.total_asset / 1000000}', '期货', 1)
  return <main className="mx-auto max-w-xl p-4">
    <h1>目标函数试跑</h1>
    <PortfolioTrialAccounts accounts={calc.accounts} accountId={calc.accountId} portfolioId={1}
      onSelect={calc.setAccountId} onLeverageChange={calc.setLeverageDraft} disabled={calc.validating}
      simulationChannels={calc.simulationChannels} simulationChannel={calc.simulationChannel}
      simulationAccount={calc.simulationAccount} onSimulationSelect={calc.setSimulationChannel} />
    <button type="button" disabled={!calc.canRun} onClick={() => void calc.run()} className="mt-4 rounded border border-line px-4 py-2">试跑</button>
    {calc.result && <p role="status">{calc.stale ? '旧结果' : '当前结果'} · {JSON.stringify(calc.result.target)}</p>}
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
