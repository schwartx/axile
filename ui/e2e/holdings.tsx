import { useCallback, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { HoldingDetailDialog } from '../src/features/account/HoldingDetailDialog'
import { HoldingsPreviewTable } from '../src/features/account/HoldingsPreviewTable'
import { holdingPreviewRows } from '../src/features/account/holdingDetailModel'
import { useCurrentOrders } from '../src/features/account/useCurrentOrders'
import { getAccountAssetSnapshots, getAccountTargetSnapshot } from '../src/lib/api/accounts'
import { usePolling } from '../src/lib/hooks/usePolling'
import '../src/styles/theme.css'

const units = { quantity_kind: 'contract', quantity_label: '手', quantity_max_decimals: 0, price_label: '元', notional_label: '元' } as const

export function Fixture() {
  const [accountId, setAccountId] = useState(1)
  const [selection, setSelection] = useState<{ accountId: number; symbol: string } | null>(null)
  const symbol = selection?.accountId === accountId ? selection.symbol : null
  const orders = useCurrentOrders(accountId, symbol != null)
  const assets = usePolling(useCallback((signal: AbortSignal) => getAccountAssetSnapshots(accountId, { limit: 1 }, signal), [accountId]), { queryKey: `assets:${accountId}`, intervalMs: 0 })
  const target = usePolling(useCallback((signal: AbortSignal) => getAccountTargetSnapshot(accountId, signal), [accountId]), { queryKey: `target:${accountId}`, intervalMs: 0 })
  const latest = assets.data?.data[0]
  const positions = latest?.assets.positions ?? []
  const refresh = () => Promise.all([assets.refresh(), target.refresh(), orders.refresh()]).then(() => undefined)
  return <main className="min-h-screen bg-bg p-6 text-ink-1">
    <button type="button" onClick={() => setAccountId(accountId === 1 ? 2 : 1)} className="mb-4">切换账户</button>
    <section className="max-w-[960px] rounded-xl border border-line bg-surface p-6">
      <h1 className="mb-4 text-sm text-ink-2">持仓 vs 目标 · 账户{accountId}</h1>
      <HoldingsPreviewTable holdings={assets.loading || target.loading ? null : holdingPreviewRows(positions, latest?.assets.total_asset ?? 0, target.data, !!latest)}
        observed={!!latest} currency="CNY" positionValueLabel="货值"
        onSelect={value => { setSelection({ accountId, symbol: value }); void orders.refresh() }} />
    </section>
    <HoldingDetailDialog symbol={symbol} positions={positions} observed={!!latest}
      observedAt={latest?.created_at ?? null} target={target.data} orders={orders} currency="CNY" units={units}
      dataStale={assets.stale || target.stale || !!assets.error || !!target.error} refreshing={assets.refreshing || target.refreshing}
      onClose={() => setSelection(null)} onRefresh={refresh} />
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
