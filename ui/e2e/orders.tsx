import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { CurrentOrdersButton, CurrentOrdersDialog } from '../src/features/account/CurrentOrders'
import { useCurrentOrders } from '../src/features/account/useCurrentOrders'
import '../src/styles/theme.css'

export function Fixture() {
  const state = useCurrentOrders(1)
  const [running, setRunning] = useState(true)
  return <main className="min-h-screen bg-bg p-6 text-ink-1">
    <button onClick={() => setRunning(!running)} className="mb-4">{running ? '结束执行' : '开始执行'}</button>
    <section className="rounded-xl border border-line bg-surface p-6">
      <h1 className="mb-4 text-sm text-ink-2">持仓 vs 目标</h1>
      <p className="text-xl font-semibold">{running ? '正在调仓' : '已调仓到位'}</p>
      <p className="mt-2 text-sm text-ink-2">当前 6 只 · 目标 6 只</p>
      <CurrentOrdersButton state={state} />
    </section>
    <CurrentOrdersDialog state={state} accountName="示例账户" units={{ quantity_kind: 'custom', quantity_label: '手', quantity_max_decimals: 0, price_label: '元', notional_label: '元' }} />
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
