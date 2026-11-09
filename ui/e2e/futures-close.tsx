import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider, Link } from 'react-router'
import { AccountEditAlgorithmPage } from '../src/pages/AccountEditAlgorithmPage'
import { useChannelCatalogStore } from '../src/stores/channels'
import { RouteWorkspace } from '../src/components/RouteWorkspace'
import '../src/styles/theme.css'

await useChannelCatalogStore.getState().refresh()
const router = createMemoryRouter([{ path: '/accounts/:id/edit/algorithm', element:
  <div className="grid h-dvh grid-rows-[auto_minmax(0,1fr)] overflow-hidden bg-bg text-ink-1">
    <nav className="flex gap-5 border-b border-line px-4 py-2"><Link to="/accounts/1/edit/algorithm">CTP 示例账户</Link><Link to="/accounts/2/edit/algorithm">TQ 示例账户</Link></nav>
    <RouteWorkspace fullBleed><AccountEditAlgorithmPage /></RouteWorkspace>
  </div>,
}], { initialEntries: ['/accounts/1/edit/algorithm'] })
createRoot(document.getElementById('root')!).render(<RouterProvider router={router} />)
