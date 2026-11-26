import { SIGNAL_CHANNEL } from '@/features/signal/signalData'
import { useEffect, useLayoutEffect, useRef, useState, type ComponentType, type CSSProperties, type ReactNode, type RefObject } from 'react'
import { matchPath, useLocation } from 'react-router'
import {
  BellRing,
  Boxes,
  CircleGauge,
  CircleUserRound,
  FolderPlus,
  Settings2,
  UserRoundPlus,
  X,
} from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { OverflowText } from '@/components/ui/OverflowText'
import { EdgeScrollHint } from '@/components/ui/EdgeScrollHint'
import { ResizeHandle } from '@/components/ui/ResizeHandle'
import { Tooltip } from '@/components/ui/Tooltip'
import { useScrollEdges } from '@/hooks/useScrollEdges'
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
const SIDEBAR_WIDTH_KEY = 'axon.sidebarWidth'
const DEFAULT_WIDTH = 184
const MIN_WIDTH = 160
const MAX_WIDTH = 320
const RAIL_WIDTH = 56
const RAIL_LINK = 'relative grid size-9 shrink-0 cursor-pointer place-items-center rounded-full hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent'
const RAIL_ACTIVE = 'bg-fill text-ink-1'
const ACCOUNT_POPOVER_ID = 'sidebar-account-navigation'

type RailItem = NavItemSpec & { icon: NonNullable<NavItemSpec['icon']>; to: string }
const MAIN_NAV: RailItem[] = [
  { label: '所有账户', icon: CircleGauge, to: '/', active: pathname => pathname === '/' },
  { label: '所有组合', icon: Boxes, to: '/portfolios', active: pathname => pathname.startsWith('/portfolios') },
]
const SETTINGS_NAV: RailItem[] = [
  { label: '系统告警', icon: BellRing, to: '/settings', active: pathname => pathname === '/settings' || pathname === '/settings/notification' },
  { label: '高级设置', icon: Settings2, to: '/settings/advanced', active: pathname => pathname === '/settings/advanced' },
]
const CREATE_NAV: RailItem[] = [
  { label: '新建账户', icon: UserRoundPlus, to: '/setup/acct/channel', active: () => false },
  { label: '新建组合', icon: FolderPlus, to: '/portfolios?new=1', active: () => false },
]

function clampSidebarWidth(width: number): number {
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.round(width)))
}

function initialSidebarWidth(): number {
  if (typeof window === 'undefined') return DEFAULT_WIDTH
  const width = Number.parseInt(window.localStorage.getItem(SIDEBAR_WIDTH_KEY) ?? '', 10)
  return Number.isFinite(width) ? clampSidebarWidth(width) : DEFAULT_WIDTH
}

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

type SettingsMovePhase = 'idle' | 'leaving' | 'placed' | 'entering'

function GlobalSettingsNav({ phase, dense }: { phase: SettingsMovePhase; dense: boolean }) {
  const hidden = phase === 'leaving' || phase === 'placed'

  return (
    <div aria-hidden={hidden ? true : undefined}>
      {SETTINGS_NAV.map((item, index) => (
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

function RailLink({ item }: { item: RailItem }) {
  const { pathname, hash } = useLocation()
  const active = item.active(pathname, hash)
  const Icon = item.icon
  return <Tooltip content={item.label}>
    <Link to={item.to} aria-label={item.label} aria-current={active ? 'page' : undefined}
      className={`${RAIL_LINK} ${active ? RAIL_ACTIVE : 'text-ink-2'}`}>
      <Icon size={18} aria-hidden />
    </Link>
  </Tooltip>
}

function SidebarRail({ accountName, accountLinks }: { accountName: string | null; accountLinks: ReactNode }) {
  const location = useLocation()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const placeAccountPanel = () => {
    const button = trigger.current
    const menu = panel.current
    if (!button || !menu || !menu.matches(':popover-open')) return
    if (!button.getClientRects().length) {
      menu.hidePopover()
      return
    }
    const anchor = button.getBoundingClientRect()
    menu.style.top = `${Math.max(8, Math.min(anchor.top, window.innerHeight - menu.offsetHeight - 8))}px`
  }
  useEffect(() => { panel.current?.hidePopover() }, [location.key, accountName])
  useEffect(() => {
    window.addEventListener('resize', placeAccountPanel)
    return () => window.removeEventListener('resize', placeAccountPanel)
  }, [])
  const accountActive = accountIdFromPath(location.pathname) != null

  return <nav aria-label="功能导航" className="hidden min-h-0 flex-1 flex-col items-center gap-2 overflow-y-auto py-0.5 md:flex">
    {MAIN_NAV.map(item => <RailLink key={item.label} item={item} />)}
    {SETTINGS_NAV.map(item => <RailLink key={item.label} item={item} />)}
    {accountName !== null && <>
      <div className="my-1 w-6 shrink-0 border-t border-line" aria-hidden />
      <Tooltip content={`当前账户 · ${accountName}`}>
        <button ref={trigger} type="button" popoverTarget={ACCOUNT_POPOVER_ID} aria-label={`当前账户 · ${accountName}`}
          aria-current={accountActive ? 'location' : undefined}
          className={`${RAIL_LINK} ${accountActive ? RAIL_ACTIVE : 'text-ink-2'}`}>
          <CircleUserRound size={18} aria-hidden />
        </button>
      </Tooltip>
      <div id={ACCOUNT_POPOVER_ID} ref={panel} popover="auto" onToggle={placeAccountPanel}
        className="fixed inset-auto left-16 m-0 max-h-[calc(100dvh-1rem)] w-60 overflow-y-auto rounded-xl border border-line bg-surface p-2 text-ink-1 shadow-[0_12px_32px_rgba(0,0,0,0.16)]">
        <div className="mb-1 border-b border-line px-2.5 pt-1 pb-2 text-sm font-semibold break-words">{accountName}</div>
        {accountLinks}
      </div>
    </>}
    <div className="mt-auto flex flex-col items-center gap-2 pt-2">
      {CREATE_NAV.map(item => <RailLink key={item.label} item={item} />)}
    </div>
  </nav>
}

function NavItem({ item, dense = false }: { item: NavItemSpec; dense?: boolean }) {
  const location = useLocation()
  const active = item.active(location.pathname, location.hash)
  const editRoot = matchPath('/accounts/:id/edit/*', location.pathname)?.pathnameBase
  const sameAccountSettings = !!editRoot && (item.to === editRoot || item.to?.startsWith(`${editRoot}/`))
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
      viewTransition={!sameAccountSettings}
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
  desktopCollapsed = false,
  mobileOpen = false,
  navigationTriggerRef,
  onClose,
}: {
  desktopCollapsed?: boolean
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
  const sidebarRef = useRef<HTMLElement>(null)
  const [width, setWidth] = useState(initialSidebarWidth)
  const [dragWidth, setDragWidth] = useState<number | null>(null)
  const saveWidth = (next: number) => {
    setWidth(next)
    window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(next))
  }

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
    if (!scrollEdges.measured || scrollEdges.clientHeight === 0 || movingSettings.current) return
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
  const signalAccount = activeAccount?.trade_channel === SIGNAL_CHANNEL
  const accountOverview: NavItemSpec[] = [
    { label: '账户概览', to: accountPath(), active: exact(accountPath()) },
    ...(signalAccount ? [
      { label: '信号记录', to: accountPath('/signals'), active: exact(accountPath('/signals')) },
    ] : [
      { label: '持仓明细', to: accountPath('/holdings'), active: exact(accountPath('/holdings')) },
      {
        label: '执行记录',
        to: accountPath('/executions'),
        active: (pathname: string) => pathname === accountPath('/executions') || pathname.startsWith(`${accountPath('/executions')}/`),
      },
      { label: '实盘绩效', to: accountPath('/history'), active: exact(accountPath('/history')) },
    ]),
  ]
  const accountParameters: NavItemSpec[] = [
    { label: '连接设置', to: accountPath('/edit/connection'), active: exact(accountPath('/edit/connection')) },
  ]
  const accountExecution: NavItemSpec[] = [
    { label: '定时任务', to: accountPath('/edit/timer'), active: exact(accountPath('/edit/timer')) },
    { label: '执行算法', to: accountPath('/edit/algorithm'), active: exact(accountPath('/edit/algorithm')) },
    { label: '执行流控', to: accountPath('/edit/control'), active: exact(accountPath('/edit/control')) },
    { label: '绑定组合', to: accountPath('/edit/portfolio'), active: exact(accountPath('/edit/portfolio')) },
  ]
  const accountLinks = <>
    <div>{accountOverview.map(item => <NavItem key={item.label} item={item} dense={denseNav} />)}</div>
    {signalAccount ? <NavSection compact dense={denseNav} label="设置">
      <NavItem item={{ label: '行情设置', to: accountPath('/edit/quotes'), active: exact(accountPath('/edit/quotes')) }} dense={denseNav} />
      <NavItem item={{ label: '资金与持仓', to: accountPath('/edit/assets'), active: exact(accountPath('/edit/assets')) }} dense={denseNav} />
      <NavItem item={accountExecution[0]} dense={denseNav} />
      <NavItem item={accountExecution[3]} dense={denseNav} />
    </NavSection> : <>
      <NavSection compact dense={denseNav} label="参数">
        {accountParameters.map(item => <NavItem key={item.label} item={item} dense={denseNav} />)}
      </NavSection>
      <NavSection compact dense={denseNav} label="执行">
        {accountExecution.map(item => <NavItem key={item.label} item={item} dense={denseNav} />)}
      </NavSection>
    </>}
  </>

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
        ref={sidebarRef}
        style={{ '--sidebar-width': `${desktopCollapsed ? RAIL_WIDTH : (dragWidth ?? width)}px` } as CSSProperties}
        className={`fixed inset-y-0 left-0 z-40 flex w-[184px] flex-none flex-col border-r border-line bg-surface py-3.5 pr-2.5 pl-3 duration-200 motion-reduce:transition-none md:relative md:z-10 md:w-[var(--sidebar-width)] md:translate-x-0 ${
          dragWidth == null ? 'transition-[width,transform]' : 'transition-transform'
        } ${desktopCollapsed ? 'md:px-1' : 'md:border-r-0'} ${
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
      {desktopCollapsed && <SidebarRail accountName={accountId == null ? null : activeAccount?.name ?? '当前账户'} accountLinks={accountLinks} />}
      <div id="desktop-account-navigation" className={`flex min-h-0 flex-1 flex-col ${desktopCollapsed ? 'md:hidden' : ''}`}>
      <div className="relative flex min-h-0 flex-1 flex-col">
        <nav ref={navRef} className="quiet-scrollbar min-h-0 flex-1 overflow-x-hidden overflow-y-auto pb-2" aria-label="功能导航">
          <div className="flex min-h-full flex-col justify-between">
            <div>
              {MAIN_NAV.map(item => <NavItem key={item.label} item={item} dense={denseNav} />)}
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
                  <div className="mb-1 flex min-w-0 items-center gap-1.5 px-2.5 pt-0.5">
                    <OverflowText
                      className="text-[14px] font-[620] text-ink-1"
                      text={activeAccount?.name ?? '当前账户'}
                    />
                    {activeAccount && (
                      <span className="max-w-[65%] shrink-0 truncate rounded-chip bg-fill px-1.5 py-px text-[11.5px] text-ink-2">
                        {channelLabel(activeAccount.trade_channel, activeAccount.market)}
                      </span>
                    )}
                  </div>
                  {accountLinks}
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
          {CREATE_NAV.map(item => <Link key={item.label} to={item.to}
            className="flex items-center justify-center whitespace-nowrap rounded-chip border border-line px-1.5 py-1.5 text-[13px] text-ink-2 hover:border-border-strong hover:text-ink-1">
            {item.label}
          </Link>)}
        </div>
      </div>
      </div>
      {!desktopCollapsed && <ResizeHandle orientation="vertical" aria-label="调整侧栏宽度"
        aria-valuetext={`${dragWidth ?? width} 像素`} value={dragWidth ?? width} min={MIN_WIDTH} max={MAX_WIDTH} step={8}
        title="左右拖动调整宽度 · 双击恢复默认" showGrip={false}
        className="absolute inset-y-0 -right-1 z-10 hidden w-2 md:block"
        valueAt={position => clampSidebarWidth(position - (sidebarRef.current?.getBoundingClientRect().left ?? 0))}
        onChange={setDragWidth} onCommit={saveWidth} onReset={() => saveWidth(DEFAULT_WIDTH)}
        onResizeChange={resizing => { if (!resizing) setDragWidth(null) }} />}
      </aside>
    </>
  )
}
