import { useEffect, useLayoutEffect, useRef, useState, type ComponentType, type CSSProperties, type ReactNode, type RefObject } from 'react'
import { useLocation } from 'react-router'
import {
  BellRing,
  Boxes,
  ChevronDown,
  ChevronUp,
  CircleGauge,
  Plus,
  Settings2,
  X,
} from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { channelLabel } from '@/features/dashboard/display'
import { useDomainStore } from '@/stores/domain'
import { useNavigationStore } from '@/stores/ui'

interface NavItemSpec {
  label: string
  icon?: ComponentType<{ size?: number; 'aria-hidden'?: boolean }>
  to: string | null
  active: (pathname: string, hash: string) => boolean
}

const activeMarkerStyle: CSSProperties = { viewTransitionName: 'app-nav-selection' }

function accountIdFromPath(pathname: string): number | null {
  const matched = pathname.match(/^\/accounts\/(\d+)(?:\/|$)/)
  if (!matched) return null
  const accountId = Number(matched[1])
  return Number.isFinite(accountId) ? accountId : null
}

function NavSection({
  label,
  children,
  compact = false,
  dense = false,
}: {
  label: string
  children: ReactNode
  /** 账户块内部使用：组间距收紧，让块内多组读起来是一个整体。 */
  compact?: boolean
  /** 侧栏溢出时进一步压缩纵向密度。 */
  dense?: boolean
}) {
  const margin = compact
    ? dense ? 'mt-2.5' : 'mt-3.5'
    : dense ? 'mt-4' : 'mt-5'
  return (
    <section className={`${margin} first:mt-0 transition-[margin] duration-200 motion-reduce:transition-none`}>
      <div className="mb-1 truncate px-2.5 text-[12px] font-semibold tracking-[0.14em] text-ink-3" title={label}>
        {label}
      </div>
      <div>{children}</div>
    </section>
  )
}

/** 跟踪滚动容器两端是否还有被裁掉的内容，用于驱动边缘渐隐提示。 */
function useScrollEdges(ref: RefObject<HTMLElement | null>): {
  up: boolean
  down: boolean
  overflowing: boolean
  measured: boolean
  scrollHeight: number
  clientHeight: number
} {
  const [edges, setEdges] = useState({ up: false, down: false, overflowing: false, measured: false, scrollHeight: 0, clientHeight: 0 })
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const content = el.firstElementChild
    const update = () => {
      const remaining = el.scrollHeight - el.clientHeight - el.scrollTop
      const next = {
        up: el.scrollTop > 1,
        down: remaining > 1,
        overflowing: el.scrollHeight - el.clientHeight > 1,
        measured: true,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      }
      setEdges((prev) => (
        prev.up === next.up
        && prev.down === next.down
        && prev.overflowing === next.overflowing
        && prev.measured === next.measured
        && prev.scrollHeight === next.scrollHeight
        && prev.clientHeight === next.clientHeight
          ? prev
          : next
      ))
    }
    update()
    el.addEventListener('scroll', update, { passive: true })
    const observer = new ResizeObserver(update)
    observer.observe(el)
    if (content) observer.observe(content)
    return () => {
      el.removeEventListener('scroll', update)
      observer.disconnect()
    }
  }, [ref])
  return edges
}

type SettingsMovePhase = 'idle' | 'leaving' | 'placed' | 'entering'

function GlobalSettingsNav({ phase, dense }: { phase: SettingsMovePhase; dense: boolean }) {
  const exact = (to: string) => (pathname: string) => pathname === to
  const hidden = phase === 'leaving' || phase === 'placed'
  const items: NavItemSpec[] = [
    { label: '系统告警', icon: BellRing, to: '/settings', active: (pathname) => pathname === '/settings' || pathname === '/settings/notification' },
    { label: '高级设置', icon: Settings2, to: '/settings/advanced', active: exact('/settings/advanced') },
  ]

  return (
    <div aria-hidden={hidden ? true : undefined}>
      {items.map((item, index) => (
        <div
          key={item.label}
          className={`sidebar-setting-row ${dense ? 'sidebar-setting-row-dense' : ''} ${hidden ? 'sidebar-setting-row-hidden' : ''}`}
          style={{ '--sidebar-setting-index': index } as CSSProperties}
          inert={hidden}
        >
          <div className={`sidebar-setting-item ${hidden ? 'sidebar-setting-item-hidden' : ''}`}>
            <NavItem item={item} dense={dense} />
          </div>
        </div>
      ))}
    </div>
  )
}

/** 溢出端的方向提示：渐隐带 + 可点击箭头，点击按接近一屏的步长朝该端滚动。 */
function EdgeScrollHint({ edge, visible, target }: { edge: 'up' | 'down'; visible: boolean; target: RefObject<HTMLElement | null> }) {
  const Icon = edge === 'up' ? ChevronUp : ChevronDown
  const nudge = () => {
    const el = target.current
    if (!el) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    el.scrollBy({
      top: (edge === 'up' ? -1 : 1) * el.clientHeight * 0.8,
      behavior: reduced ? 'auto' : 'smooth',
    })
  }
  return (
    <div
      aria-hidden={!visible}
      className={`pointer-events-none absolute inset-x-0 z-[2] flex h-5 from-surface via-surface/80 to-transparent transition-opacity duration-200 motion-reduce:transition-none ${
        edge === 'up' ? 'top-0 items-start bg-gradient-to-b' : 'bottom-0 items-end bg-gradient-to-t'
      } ${visible ? 'opacity-100' : 'opacity-0'}`}
    >
      <button
        type="button"
        aria-label={edge === 'up' ? '向上滚动导航' : '向下滚动导航'}
        tabIndex={visible ? 0 : -1}
        onClick={nudge}
        className={`mx-auto px-3 text-ink-3 transition-colors duration-150 hover:text-ink-1 ${
          visible ? 'pointer-events-auto' : 'pointer-events-none'
        }`}
      >
        <Icon size={14} aria-hidden />
      </button>
    </div>
  )
}

function NavItem({ item, dense = false }: { item: NavItemSpec; dense?: boolean }) {
  const location = useLocation()
  const active = item.active(location.pathname, location.hash)
  const Icon = item.icon
  const content = (
    <>
      {/* 游标：贴左缘的青色指示条，跨入口滑移（共享元素 FLIP）——「当前位置」是仪表上的游标，不是高亮块。 */}
      {active && (
        <span aria-hidden className="absolute top-1 bottom-1 left-0 w-[2.5px] rounded-full bg-accent" style={activeMarkerStyle} />
      )}
      <span className="relative z-[1] flex items-center gap-2.5">
        {Icon && <Icon size={16} aria-hidden />}
        <span>{item.label}</span>
      </span>
    </>
  )

  if (!item.to) {
    return (
      <span className={`relative flex items-center gap-2 px-2.5 text-[14px] text-ink-3 transition-[height] duration-200 motion-reduce:transition-none ${dense ? 'h-[30px]' : 'h-8.5'}`} aria-disabled="true">
        {content}
      </span>
    )
  }

  return (
    <Link
      to={item.to}
      aria-current={active ? 'page' : undefined}
      className={`relative flex items-center gap-2 px-2.5 text-[14px] transition-[height,color] duration-200 motion-reduce:transition-none ${dense ? 'h-[30px]' : 'h-8.5'} ${
        active ? 'font-[550] text-ink-1' : 'text-ink-2 hover:text-ink-1'
      }`}
    >
      {content}
    </Link>
  )
}

/** 悬浮式常驻导航：层级只表达归属，每个二级入口均直接抵达最终页面。 */
export function AppSidebar({
  mobileOpen = false,
  navigationTriggerRef,
  onClose,
}: {
  mobileOpen?: boolean
  navigationTriggerRef?: RefObject<HTMLButtonElement | null>
  onClose?: () => void
}) {
  const location = useLocation()
  const accounts = useDomainStore((state) => state.accounts)
  const activeAccountId = useNavigationStore((state) => state.activeAccountId)
  const setActiveAccountId = useNavigationStore((state) => state.setActiveAccountId)
  const setSidebarCompact = useNavigationStore((state) => state.setSidebarCompact)
  const routeAccountId = accountIdFromPath(location.pathname)

  useEffect(() => {
    if (!accounts) return
    const ids = new Set(accounts.map((account) => account.account_id))
    const next = routeAccountId != null && ids.has(routeAccountId)
      ? routeAccountId
      : activeAccountId != null && ids.has(activeAccountId)
        ? activeAccountId
        : (accounts[0]?.account_id ?? null)
    if (next !== activeAccountId) setActiveAccountId(next)
  }, [accounts, routeAccountId, activeAccountId, setActiveAccountId])

  useEffect(() => {
    onClose?.()
  }, [location.pathname, onClose])

  const accountId = routeAccountId ?? activeAccountId
  const accountPath = (suffix = '') => accountId == null ? null : `/accounts/${accountId}${suffix}`
  const exact = (to: string | null) => (pathname: string) => to != null && pathname === to
  // 账户段身份：块头直接署名当前账户（名称 + 渠道），本段所有条目都属于它。
  const activeAccount = accounts?.find((a) => a.account_id === accountId)
  // 库里确定没有账户时收拢账户导航：三组入口此刻只有占位价值，一行说明它的归宿比 12 个禁用项安静。
  const noAccounts = accounts != null && accounts.length === 0 && accountId == null

  const navRef = useRef<HTMLElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const wasMobileOpen = useRef(mobileOpen)
  const scrollEdges = useScrollEdges(navRef)
  const [settingsAtBottom, setSettingsAtBottom] = useState(false)
  const [denseNav, setDenseNav] = useState(false)
  const [settingsMovePhase, setSettingsMovePhase] = useState<SettingsMovePhase>('idle')
  const settingsAtBottomRef = useRef(false)
  const denseNavRef = useRef(false)
  const naturalNavHeight = useRef(0)
  const overflowInitialized = useRef(false)
  const movingSettings = useRef(false)
  const settingsMoveTimers = useRef<number[]>([])

  useEffect(() => {
    if (mobileOpen) closeButtonRef.current?.focus()
    else if (wasMobileOpen.current) navigationTriggerRef?.current?.focus()
    wasMobileOpen.current = mobileOpen
  }, [mobileOpen, navigationTriggerRef])

  useEffect(() => {
    if (!mobileOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose?.()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [mobileOpen, onClose])

  useLayoutEffect(() => {
    if (!scrollEdges.measured || movingSettings.current) return
    const shouldCompact = denseNavRef.current
      ? scrollEdges.clientHeight < naturalNavHeight.current - 1
      : scrollEdges.overflowing
    if (!overflowInitialized.current) {
      overflowInitialized.current = true
      if (shouldCompact) naturalNavHeight.current = scrollEdges.scrollHeight
      denseNavRef.current = shouldCompact
      settingsAtBottomRef.current = shouldCompact
      setDenseNav(shouldCompact)
      setSidebarCompact(shouldCompact)
      setSettingsAtBottom(shouldCompact)
      return
    }
    if (shouldCompact === settingsAtBottomRef.current) return

    if (shouldCompact) naturalNavHeight.current = scrollEdges.scrollHeight
    denseNavRef.current = shouldCompact
    setDenseNav(shouldCompact)
    setSidebarCompact(shouldCompact)

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      settingsAtBottomRef.current = shouldCompact
      setSettingsAtBottom(shouldCompact)
      return
    }

    const targetAtBottom = shouldCompact
    movingSettings.current = true
    setSettingsMovePhase('leaving')
    const placeTimer = window.setTimeout(() => {
      settingsAtBottomRef.current = targetAtBottom
      setSettingsAtBottom(targetAtBottom)
      setSettingsMovePhase('placed')
      requestAnimationFrame(() => requestAnimationFrame(() => setSettingsMovePhase('entering')))
    }, 270)
    const finishTimer = window.setTimeout(() => {
      setSettingsMovePhase('idle')
      movingSettings.current = false
    }, 540)
    settingsMoveTimers.current = [placeTimer, finishTimer]
  }, [scrollEdges.clientHeight, scrollEdges.measured, scrollEdges.overflowing, scrollEdges.scrollHeight, setSidebarCompact])

  useEffect(() => () => {
    settingsMoveTimers.current.forEach((timer) => window.clearTimeout(timer))
    setSidebarCompact(false)
  }, [setSidebarCompact])

  // 账户段条目不带图标：归属已由「身份块头 + 左缘线 + 缩进」表达，段内保持素净。
  const accountOverview: NavItemSpec[] = [
    { label: '账户概览', to: accountPath(), active: exact(accountPath()) },
    { label: '持仓明细', to: accountPath('/holdings'), active: exact(accountPath('/holdings')) },
    {
      label: '执行记录',
      to: accountPath('/executions'),
      active: (pathname) => pathname === accountPath('/executions') || pathname.startsWith(`${accountPath('/executions')}/`),
    },
    { label: '实盘绩效', to: accountPath('/history'), active: exact(accountPath('/history')) },
  ]
  const accountParameters: NavItemSpec[] = [
    { label: '基本信息', to: accountPath('/edit'), active: exact(accountPath('/edit')) },
    { label: '连接设置', to: accountPath('/edit/connection'), active: exact(accountPath('/edit/connection')) },
    { label: '杠杆设置', to: accountPath('/edit/leverage'), active: exact(accountPath('/edit/leverage')) },
    { label: '品种控制', to: accountPath('/edit/symbols'), active: exact(accountPath('/edit/symbols')) },
  ]
  const accountExecution: NavItemSpec[] = [
    { label: '定时任务', to: accountPath('/edit/timer'), active: exact(accountPath('/edit/timer')) },
    { label: '执行算法', to: accountPath('/edit/algorithm'), active: exact(accountPath('/edit/algorithm')) },
    { label: '执行流控', to: accountPath('/edit/control'), active: exact(accountPath('/edit/control')) },
    { label: '绑定组合', to: accountPath('/edit/portfolio'), active: exact(accountPath('/edit/portfolio')) },
  ]

  return (
    <>
      <button
        type="button"
        aria-label="关闭主导航"
        tabIndex={mobileOpen ? 0 : -1}
        onClick={onClose}
        className={`fixed inset-0 z-30 bg-ink-1/20 transition-opacity duration-200 motion-reduce:transition-none md:hidden ${
          mobileOpen ? 'opacity-100' : 'pointer-events-none opacity-0'
        }`}
      />
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[184px] flex-none flex-col border-r border-line bg-surface py-3.5 pr-2.5 pl-3 transition-transform duration-200 motion-reduce:transition-none md:static md:z-auto md:translate-x-0 ${
          mobileOpen ? 'visible translate-x-0' : 'invisible -translate-x-full md:visible'
        }`}
        aria-label="主导航"
      >
      <div className="mb-2 flex items-center justify-between px-2.5 md:hidden">
        <span className="text-[13px] font-medium text-ink-2">导航</span>
        <button ref={closeButtonRef} type="button" aria-label="关闭主导航" className="text-ink-2 hover:text-ink-1" onClick={onClose}>
          <X size={18} aria-hidden />
        </button>
      </div>
      <div className="relative flex min-h-0 flex-1 flex-col">
        <nav ref={navRef} className="quiet-scrollbar min-h-0 flex-1 overflow-y-auto pb-2" aria-label="功能导航">
          <div className="flex min-h-full flex-col justify-between">
            <div>
              <NavItem dense={denseNav} item={{ label: '所有账户', icon: CircleGauge, to: '/', active: (pathname) => pathname === '/' }} />
              <NavItem dense={denseNav} item={{ label: '所有组合', icon: Boxes, to: '/portfolios', active: (pathname) => pathname.startsWith('/portfolios') }} />
              {!settingsAtBottom && <GlobalSettingsNav phase={settingsMovePhase} dense={denseNav} />}

              {/* 横线分割：顶层（全局与系统设置）与账户段各自成块。 */}
              <div className={`${denseNav ? 'mt-3' : 'mt-4'} border-t border-line transition-[margin] duration-200 motion-reduce:transition-none`} />
              {noAccounts ? (
                <NavSection label="当前账户" dense={denseNav}>
                  <p className="px-2.5 py-1.5 text-[13px] leading-relaxed text-ink-3">
                    创建账户后，这里会出现它的持仓、参数与执行入口。
                  </p>
                </NavSection>
              ) : (
                /* 账户段：身份块头（账户名 + 渠道）+ 左缘贯通 hairline + 统一缩进——
                   整段条目属于这个账户。不用围合面板，层次靠线。 */
                <div className={`${denseNav ? 'mt-3' : 'mt-4'} ml-1 border-l border-line pl-1 transition-[margin] duration-200 motion-reduce:transition-none`}>
                  <div className="mb-1 flex min-w-0 items-center gap-1.5 px-2.5 pt-0.5" title={activeAccount?.name ?? '当前账户'}>
                    <span className="truncate text-[14px] font-[620] text-ink-1">
                      {activeAccount?.name ?? '当前账户'}
                    </span>
                    {activeAccount && (
                      <span className="flex-none rounded-chip bg-fill px-1.5 py-px text-[11.5px] text-ink-2">
                        {channelLabel(activeAccount.trade_channel, activeAccount.market)}
                      </span>
                    )}
                  </div>
                  <div>
                    {accountOverview.map((item) => <NavItem key={item.label} item={item} dense={denseNav} />)}
                  </div>
                  <NavSection compact dense={denseNav} label="参数">
                    {accountParameters.map((item) => <NavItem key={item.label} item={item} dense={denseNav} />)}
                  </NavSection>
                  <NavSection compact dense={denseNav} label="执行">
                    {accountExecution.map((item) => <NavItem key={item.label} item={item} dense={denseNav} />)}
                  </NavSection>
                </div>
              )}
              {settingsAtBottom && (
                <div className={`${denseNav ? 'mt-3 pt-3' : 'mt-4 pt-4'} border-t border-line transition-[margin,padding] duration-200 motion-reduce:transition-none`}>
                  <GlobalSettingsNav phase={settingsMovePhase} dense={denseNav} />
                </div>
              )}
            </div>
          </div>
        </nav>
        {/* 溢出揭示：被裁掉的一端用渐隐 + 方向箭头暗示「还有下文」，滚到顶/底即消失。常挂 + 透明度切换，不条件挂载。 */}
        <EdgeScrollHint edge="up" visible={scrollEdges.up} target={navRef} />
        <EdgeScrollHint edge="down" visible={scrollEdges.down} target={navRef} />
      </div>

      <div className="mt-3 border-t border-line pt-3">
        <div className="grid grid-cols-2 gap-1.5">
          <Link to="/setup/acct/channel" className="flex items-center justify-center gap-1.5 rounded-chip border border-line px-2 py-1.5 text-[13px] text-ink-2 hover:border-border-strong hover:text-ink-1">
            <Plus size={14} aria-hidden />新建账户
          </Link>
          <Link to="/portfolios?new=1" className="flex items-center justify-center gap-1.5 rounded-chip border border-line px-2 py-1.5 text-[13px] text-ink-2 hover:border-border-strong hover:text-ink-1">
            <Plus size={14} aria-hidden />新建组合
          </Link>
        </div>
      </div>
      </aside>
    </>
  )
}
