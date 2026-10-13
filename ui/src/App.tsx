import { useEffect, useState } from 'react'
import { createBrowserRouter, Navigate, Outlet, RouterProvider, ScrollRestoration } from 'react-router'
import { AppShell } from '@/components/AppShell'
import { cancelCurveTransition, observeCurveNavigation } from '@/features/history/curveTransition'
import { InitWizard } from '@/features/init/InitWizard'
import { initStatus, initValuesFromStatus, type InitValues } from '@/lib/api/init'
import { useDomainSync } from '@/stores/domain'
import { useChannelCatalogSync } from '@/stores/channels'
import { useLiveControlSync } from '@/lib/api/controlStream'
import { useLiveExecSync } from '@/lib/api/executionStream'
import { DashboardPage } from '@/pages/DashboardPage'
import { AccountDetailPage } from '@/pages/AccountDetailPage'
import { AccountEditPage } from '@/pages/AccountEditPage'
import { AccountEditConnectionPage } from '@/pages/AccountEditConnectionPage'
import { AccountEditTimerPage } from '@/pages/AccountEditTimerPage'
import { AccountEditAlgorithmPage } from '@/pages/AccountEditAlgorithmPage'
import { AccountEditNotificationPage } from '@/pages/AccountEditNotificationPage'
import { AccountEditControlPage } from '@/pages/AccountEditControlPage'
import { AccountHoldingsPage } from '@/pages/AccountHoldingsPage'
import { ExecutionDetailPage } from '@/pages/ExecutionDetailPage'
import { AccountHistoryPage } from '@/pages/AccountHistoryPage'
import { AccountExecutionsPage } from '@/pages/AccountExecutionsPage'
import { PortfoliosPage } from '@/pages/PortfoliosPage'
import { PortfolioEditPage } from '@/pages/PortfolioEditPage'
import { Placeholder } from '@/pages/Placeholder'
import { SystemEditNotificationPage } from '@/pages/SystemEditNotificationPage'
import { SystemNotificationDocPage } from '@/pages/docs/SystemNotificationDocPage'
import { SystemConfigPage } from '@/pages/SystemConfigPage'
import { WizardLayout } from '@/features/setup/WizardLayout'
import { SetupHub } from '@/pages/setup/SetupHub'
import { CustomCalcDocPage } from '@/pages/docs/CustomCalcDocPage'
import { NotificationDocPage } from '@/pages/docs/NotificationDocPage'
import {
  AcctChannel,
  AcctConnect,
  AcctPortfolio,
  AcctTrade,
  AcctTimer,
  AcctConfirm,
} from '@/pages/setup/AccountSteps'

/** 已完成初始化后的正常应用：单点驱动共享领域状态轮询 + 执行实时态 SSE + 滚动复位/恢复。跨两个布局分支。 */
function ConfiguredApp() {
  useChannelCatalogSync()
  useDomainSync()
  useLiveExecSync()
  useLiveControlSync()
  return (
    <>
      <ScrollRestoration />
      <Outlet />
    </>
  )
}

/** 全屏启动占位。 */
function Splash() {
  return (
    <div className="grid h-screen place-items-center bg-bg text-ink-3">
      <div className="text-[16px] tracking-wide">axile 启动中…</div>
    </div>
  )
}

/**
 * 应用根：首屏先探测初始化状态。
 * 未完成初始化时整屏渲染初始化向导；否则进入正常应用。
 */
function AppRoot() {
  const [phase, setPhase] = useState<'loading' | 'setup' | 'ready'>('loading')
  const [initial, setInitial] = useState<InitValues | null>(null)

  useEffect(() => {
    let alive = true
    initStatus()
      .then((s) => {
        if (!alive) return
        setInitial(initValuesFromStatus(s.values))
        setPhase(s.configured ? 'ready' : 'setup')
      })
      .catch(() => {
        // 探测失败（如后端重启中）时退回正常应用，由其自身错误提示处理。
        if (alive) setPhase('ready')
      })
    return () => {
      alive = false
    }
  }, [])

  if (phase === 'loading') return <Splash />
  if (phase === 'setup' && initial) return <InitWizard initial={initial} />
  return <ConfiguredApp />
}

const router = createBrowserRouter([
  {
    element: <AppRoot />,
    children: [
      {
        element: <AppShell />,
        children: [
          { index: true, element: <DashboardPage /> },
          { path: 'accounts/:id', element: <AccountDetailPage /> },
          { path: 'accounts/:id/edit', element: <AccountEditPage /> },
          { path: 'accounts/:id/edit/connection', element: <AccountEditConnectionPage /> },
          { path: 'accounts/:id/edit/leverage', element: <AccountEditPage section="leverage" /> },
          { path: 'accounts/:id/edit/symbols', element: <AccountEditPage section="symbols" /> },
          { path: 'accounts/:id/edit/portfolio', element: <AccountEditPage section="portfolio" /> },
          { path: 'accounts/:id/edit/timer', element: <AccountEditTimerPage /> },
          { path: 'accounts/:id/edit/algorithm', element: <AccountEditAlgorithmPage /> },
          { path: 'accounts/:id/edit/notification', element: <AccountEditNotificationPage />, handle: { fullBleed: true } },
          { path: 'accounts/:id/edit/control', element: <AccountEditControlPage /> },
          { path: 'accounts/:id/holdings', element: <AccountHoldingsPage /> },
          { path: 'accounts/:id/history', element: <AccountHistoryPage /> },
          { path: 'accounts/:id/executions', element: <AccountExecutionsPage /> },
          { path: 'accounts/:id/executions/:executionId', element: <ExecutionDetailPage /> },
          { path: 'portfolios', element: <PortfoliosPage /> },
          { path: 'portfolios/:id/edit', element: <PortfolioEditPage />, handle: { fullBleed: true } },
          { path: 'settings/notification', element: <SystemEditNotificationPage />, handle: { fullBleed: true } },
          { path: 'settings', element: <SystemConfigPage /> },
          { path: 'settings/advanced', element: <SystemConfigPage section="advanced" /> },
          { path: '*', element: <Placeholder title="页面不存在" milestone="—" /> },
        ],
      },
      { path: 'docs/custom-calc', element: <CustomCalcDocPage /> },
      { path: 'docs/system-notification', element: <SystemNotificationDocPage /> },
      { path: 'docs/notification', element: <NotificationDocPage /> },
      {
        path: 'setup',
        element: <WizardLayout />,
        children: [
          { index: true, element: <SetupHub /> },
          { path: 'acct/channel', element: <AcctChannel /> },
          { path: 'acct/connect', element: <AcctConnect /> },
          { path: 'acct/portfolio', element: <AcctPortfolio /> },
          { path: 'acct/trade', element: <AcctTrade /> },
          { path: 'acct/timer', element: <AcctTimer /> },
          { path: 'acct/confirm', element: <AcctConfirm /> },
          { path: '*', element: <Navigate to="/setup" replace /> },
        ],
      },
    ],
  },
])

observeCurveNavigation(router.state.location, router.state.historyAction)
const unsubscribeCurveNavigation = router.subscribe(state => observeCurveNavigation(state.location, state.historyAction))
if (import.meta.hot) import.meta.hot.dispose(() => { unsubscribeCurveNavigation(); cancelCurveTransition() })

export default function App() {
  return <RouterProvider router={router} />
}
