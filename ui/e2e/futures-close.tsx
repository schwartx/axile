import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider, Link } from 'react-router'
import { AccountEditAlgorithmPage } from '../src/pages/AccountEditAlgorithmPage'
import { useChannelCatalogStore } from '../src/stores/channels'
import '../src/styles/theme.css'

await useChannelCatalogStore.getState().refresh()
const router = createMemoryRouter([{ path: '/accounts/:id/edit/algorithm', element:
  <main className="min-h-screen bg-bg px-6 py-8 text-ink-1">
    <nav className="mb-6 flex gap-5"><Link to="/accounts/1/edit/algorithm">CTP 示例账户</Link><Link to="/accounts/2/edit/algorithm">TQ 示例账户</Link></nav>
    <AccountEditAlgorithmPage />
  </main>,
}], { initialEntries: ['/accounts/1/edit/algorithm'] })
createRoot(document.getElementById('root')!).render(<RouterProvider router={router} />)
