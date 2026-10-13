import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { AccountControlSummary } from '../src/features/account/ControlMonitor'
import { ControlScopeUsage, ControlUsageNotice } from '../src/features/account/ControlUsage'
import { useLiveControlSync } from '../src/lib/api/controlStream'
import { useLiveControl } from '../src/stores/liveControl'
import '../src/styles/theme.css'
export function Fixture() {
  useLiveControlSync()
  const frame = useLiveControl((state) => state.frames[1])
  return <main className="p-5">
    <div onClick={() => { document.getElementById('navigation')!.textContent = 'navigated' }}><h1>示例账户</h1><AccountControlSummary accountId={1} /></div>
    <ControlUsageNotice frame={frame} /><div className="my-4 rounded-xl border border-line bg-surface p-4"><span>下单 · 账户合计 20 次/分钟 · 500 次/日</span><ControlScopeUsage frame={frame} operation="place_order" /></div><p id="navigation">未进入账户详情</p>
    <button type="button" onClick={() => useLiveControl.getState().disconnect()}>模拟断连</button>
  </main>
}
createRoot(document.getElementById('root')!).render(<BrowserRouter><Fixture /></BrowserRouter>)
