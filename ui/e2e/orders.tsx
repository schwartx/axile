import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { CurrentOrdersButton, CurrentOrdersDialog, CurrentOrdersContent } from '../src/features/account/CurrentOrders'
import { useCurrentOrders } from '../src/features/account/useCurrentOrders'
import { useBusinessSync } from '../src/lib/api/businessStream'
import '../src/styles/theme.css'

export function Fixture() {
  useBusinessSync()
  const state = useCurrentOrders(1)
  const [running, setRunning] = useState(true)
  return <main className="min-h-screen bg-bg p-6 text-ink-1">
    <button onClick={() => setRunning(!running)} className="mb-4">{running ? '结束执行' : '开始执行'}</button>
    {(['partial', 'disconnect', 'recover'] as const).map((action, index) => <button key={action} type="button" className="ml-4"
      onClick={() => void fetch(`/api/v1/test/orders/${action}`, { method: 'POST' })}>{['模拟部分成交', '模拟渠道断线', '模拟恢复补齐'][index]}</button>)}
    <section className="rounded-xl border border-line bg-surface p-6">
      <h1 className="mb-4 text-sm text-ink-2">持仓 vs 目标</h1>
      <p className="text-xl font-semibold">{running ? '正在调仓' : '已调仓到位'}</p>
      <p className="mt-2 text-sm text-ink-2">当前 6 只 · 目标 6 只</p>
      <CurrentOrdersButton state={state} />
    </section>
    {new URLSearchParams(location.search).has('compact') && <section aria-label="品种委托" className="mt-4 rounded-xl border border-line bg-surface p-4">
      <CurrentOrdersContent state={state} compact symbolFilter={symbol => symbol === 'rb2610'} />
    </section>}
    <CurrentOrdersDialog state={state} units={{ quantity_kind: 'custom', quantity_label: '手', quantity_max_decimals: 0, price_label: '元', notional_label: '元' }} />
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
