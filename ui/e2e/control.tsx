import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { AccountControlSummary } from '../src/features/account/ControlMonitor'
import { ControlRecentRecords, ControlWaitingSection } from '../src/features/account/ControlActivity'
import { ControlScopeUsage, ControlUsageNotice } from '../src/features/account/ControlUsage'
import { TimerQuickModal } from '../src/features/setup/TimerQuickModal'
import { useChannelCatalogStore } from '../src/stores/channels'
import type { ChannelCapability } from '../src/types/api'
import { useBusinessSync } from '../src/lib/api/businessStream'
import { useLiveControl } from '../src/stores/liveControl'
import '../src/styles/theme.css'
useChannelCatalogStore.setState({ channels: [{ channel: 'ctp', schedule: { kind: 'cn_futures', night: null } } as ChannelCapability] })
export function Fixture() {
  const [timerOpen, setTimerOpen] = useState(false)
  useBusinessSync()
  const frame = useLiveControl((state) => state.frames[1])
  return <main className="p-5">
    <div onClick={() => { document.getElementById('navigation')!.textContent = 'navigated' }}><h1>示例账户</h1><AccountControlSummary accountId={1} /></div>
    <ControlUsageNotice frame={frame} /><div className="my-4 rounded-xl border border-line bg-surface p-4"><span>下单 · 账户合计 20 次/分钟 · 500 次/日</span><ControlScopeUsage frame={frame} operation="place_order" /></div><p id="navigation">未进入账户详情</p>
    <ControlWaitingSection frame={frame} operationNames={{ place_order: '下单', cancel_order: '撤单' }} />
    <ControlRecentRecords frame={frame} operationNames={{ place_order: '下单', cancel_order: '撤单' }} />
    <button type="button" onClick={() => setTimerOpen(true)}>调整自动执行</button>
    <TimerQuickModal open={timerOpen} accountId={1} accountName="示例账户" tradeChannel="ctp" cronExpr="" onClose={() => setTimerOpen(false)} onSaved={() => {}} />
    <button type="button" onClick={() => { const store = useLiveControl.getState(); store.disconnect(); useLiveControl.setState({ disconnectedAt: Date.now() - 5001 }); store.expire(Date.now()) }}>模拟断连</button>
  </main>
}
createRoot(document.getElementById('root')!).render(<BrowserRouter><Fixture /></BrowserRouter>)
