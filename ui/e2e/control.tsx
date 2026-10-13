import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { AccountControlSummary, AccountControlLive } from '../src/features/account/ControlMonitor'
import { useLiveControlSync } from '../src/lib/api/controlStream'
import { useLiveControl } from '../src/stores/liveControl'
import '../src/styles/theme.css'
export function Fixture() {
  useLiveControlSync()
  return <main className="p-5">
    <div onClick={() => { document.getElementById('navigation')!.textContent = 'navigated' }}><h1>示例账户</h1><AccountControlSummary accountId={1} /></div>
    <AccountControlLive accountId={1} /><p id="navigation">未进入账户详情</p>
    <button type="button" onClick={() => useLiveControl.getState().disconnect()}>模拟断连</button>
  </main>
}
createRoot(document.getElementById('root')!).render(<BrowserRouter><Fixture /></BrowserRouter>)
