import { GlobalErrorContext } from '@/lib/errorPresentation'
import { useCallback, useRef, useState } from 'react'
import { Outlet, useLocation, useMatches } from 'react-router'
import { AppSidebar } from '@/components/AppSidebar'
import { TopBar } from '@/components/TopBar'
import { Toast } from '@/components/Toast'
import { RouteWorkspace } from '@/components/RouteWorkspace'

/** 应用外壳：状态带、贴边导航栏与独立滚动工作区——一体化驾驶舱框架。 */
export function AppShell() {
  const [navigationOpen, setNavigationOpen] = useState(false)
  const navigationTriggerRef = useRef<HTMLButtonElement>(null)
  const closeNavigation = useCallback(() => setNavigationOpen(false), [])
  const location = useLocation()
  // fullBleed 路由（工作台页）不要底部留白：页面自身 h-full 吃满视口，留白只会成为死空隙。
  const fullBleed = useMatches().some(
    (match) => (match.handle as { fullBleed?: boolean } | undefined)?.fullBleed === true,
  )

  return (
    <GlobalErrorContext.Provider value={true}><div className="flex h-full flex-col overflow-hidden bg-bg">
      <TopBar navigationTriggerRef={navigationTriggerRef} onOpenNavigation={() => setNavigationOpen(true)} />
      <div className="flex min-h-0 flex-1">
        <AppSidebar
          mobileOpen={navigationOpen}
          navigationTriggerRef={navigationTriggerRef}
          onClose={closeNavigation}
        />
        {/* 内容列在导航栏右侧的余量里居中；不设全局最小宽，窄视口（高 zoom）下
            内容自然收缩，只留纵向滚动。 */}
        {/* 路径切换才更换滚动容器；同页查询参数与后台数据更新保留当前视口。 */}
        <RouteWorkspace key={location.pathname} fullBleed={fullBleed}>
          <Outlet />
        </RouteWorkspace>
      </div>
      <Toast />
    </div></GlobalErrorContext.Provider>
  )
}
