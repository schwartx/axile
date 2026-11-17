import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { PortfolioEditPage } from '../src/pages/PortfolioEditPage'
import { SystemAlertPage } from '../src/pages/SystemAlertPage'
import { SystemEditNotificationPage } from '../src/pages/SystemEditNotificationPage'
import { AccountEditPage } from '../src/pages/AccountEditPage'
import { AccountEditNotificationPage } from '../src/pages/AccountEditNotificationPage'
import { useDomainStore } from '../src/stores/domain'
import { useChannelCatalogStore } from '../src/stores/channels'
import { simulationChannels } from './simulationAccountFixture'
import '../src/styles/theme.css'

useDomainStore.setState({ portfolios: [], accounts: [] })
useChannelCatalogStore.setState({ channels: simulationChannels, loading: false })
const params = new URLSearchParams(location.search)
const path = params.has('portfolio') ? '/portfolios/1/edit' : params.has('system-settings') ? '/settings' : params.has('system') ? '/settings/notification' : params.has('editor') ? '/accounts/1/edit/notification' : '/accounts/1/edit'
const router = createMemoryRouter([
  { path: '/portfolios/:id/edit', element: <PortfolioEditPage /> },
  { path: '/settings', element: <SystemAlertPage /> },
  { path: '/settings/notification', element: <SystemEditNotificationPage /> },
  { path: '/accounts/:id/edit', element: <AccountEditPage /> },
  { path: '/accounts/:id/edit/notification', element: <AccountEditNotificationPage /> },
], { initialEntries: [path] })
createRoot(document.getElementById('root')!).render(<RouterProvider router={router} />)
