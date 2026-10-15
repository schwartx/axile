import { GlobalErrorContext } from '@/lib/errorPresentation'
import { useCallback, useRef, useState } from 'react'
import { Outlet, useMatches } from 'react-router'
import { AppSidebar } from '@/components/AppSidebar'
import { TopBar } from '@/components/TopBar'
import { Toast } from '@/components/Toast'

/** 应用外壳：状态带、贴边导航栏与独立滚动工作区——一体化驾驶舱框架。 */
export function AppShell() {
  const [navigationOpen, setNavigationOpen] = useState(false)
  const navigationTriggerRef = useRef<HTMLButtonElement>(null)
  const closeNavigation = useCallback(() => setNavigationOpen(false), [])
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
        <main
          className={`app-workspace h-full min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable] ${
            fullBleed ? '' : 'px-4 md:px-10'
          }`}
        >
          <div className={`mx-auto h-full w-full max-w-[2176px] ${fullBleed ? '' : 'pb-16 pt-5'}`}>
            <Outlet />
          </div>
        </main>
      </div>
      <Toast />
    </div></GlobalErrorContext.Provider>
  )
}
