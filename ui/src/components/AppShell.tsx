import { GlobalErrorContext } from '@/lib/errorPresentation'
import { useCallback, useRef, useState } from 'react'
import { Outlet, useLocation, useMatches } from 'react-router'
import { AppSidebar } from '@/components/AppSidebar'
import { TopBar } from '@/components/TopBar'
import { Toast } from '@/components/Toast'
import { RouteWorkspace } from '@/components/RouteWorkspace'
import { ControlWarningPanel } from '@/features/account/ControlWarningPanel'

const SIDEBAR_COLLAPSED_KEY = 'axon.sidebarCollapsed'

/** 应用外壳：状态带、贴边导航栏与独立滚动工作区——一体化驾驶舱框架。 */
export function AppShell() {
  const [navigationOpen, setNavigationOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() =>
    typeof window !== 'undefined' && window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true',
  )
  const toggleSidebar = () => {
    const collapsed = !sidebarCollapsed
    setSidebarCollapsed(collapsed)
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed))
  }
  const navigationTriggerRef = useRef<HTMLButtonElement>(null)
  const closeNavigation = useCallback(() => setNavigationOpen(false), [])
  const location = useLocation()
  // fullBleed 路由（工作台页）不要底部留白：页面自身 h-full 吃满视口，留白只会成为死空隙。
  const matches = useMatches()
  const accountSettings = matches.find((match) => match.id === 'account-settings')
  const fullBleed = matches.some(
    (match) => (match.handle as { fullBleed?: boolean } | undefined)?.fullBleed === true,
  )

  return (
    <GlobalErrorContext.Provider value={true}><div className="flex h-full flex-col overflow-hidden bg-bg">
      <TopBar navigationTriggerRef={navigationTriggerRef} onOpenNavigation={() => setNavigationOpen(true)}
        sidebarCollapsed={sidebarCollapsed} onToggleSidebar={toggleSidebar} />
      <div className="flex min-h-0 flex-1">
        <AppSidebar
          desktopCollapsed={sidebarCollapsed}
          mobileOpen={navigationOpen}
          navigationTriggerRef={navigationTriggerRef}
          onClose={closeNavigation}
        />
        {/* 内容列在导航栏右侧的余量里居中；不设全局最小宽，窄视口（高 zoom）下
            内容自然收缩，只留纵向滚动。 */}
        {/* 同一账户的设置子页共用工作区和账户读取，仍由路由工作区管理各页滚动位置。 */}
        <RouteWorkspace key={accountSettings?.pathname.replace(/\/$/, '') ?? location.pathname} fullBleed={fullBleed}>
          <Outlet />
        </RouteWorkspace>
      </div>
      <Toast />
      <ControlWarningPanel />
    </div></GlobalErrorContext.Provider>
  )
}
