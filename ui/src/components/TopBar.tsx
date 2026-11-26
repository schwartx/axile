import { ToolsButton } from '@/features/tools/ToolsButton'
import { TimeCheckButton } from '@/features/system/timeCheck/TimeCheckButton'
import { type RefObject } from 'react'
import { Link } from '@/components/ui/nav'
import { useDomainStore } from '@/stores/domain'
import { needsAdjustmentAttention } from '@/lib/derive'
import { ThemeToggle } from '@/components/ThemeToggle'
import { Menu, PanelLeftClose, PanelLeftOpen, TriangleAlert } from 'lucide-react'
import { ServiceConnectionStatus } from '@/components/ServiceConnectionStatus'
import { BrandWordmark } from '@/components/brand/BrandWordmark'
import { Tooltip } from '@/components/ui/Tooltip'

/**
 * 顶栏 —— 品牌、后端活性点、风险提示与主题切换。
 *
 * 活性点/新鲜度直接来自共享账户数据的刷新（比单独 ping health 更诚实）：
 * 有数据且最近一次刷新无误=中性+「数据 N 秒前」；刷新出错=琥珀并保留原因。
 */
export function TopBar({
  navigationTriggerRef,
  onOpenNavigation,
  sidebarCollapsed,
  onToggleSidebar,
}: {
  navigationTriggerRef?: RefObject<HTMLButtonElement | null>
  onOpenNavigation?: () => void
  sidebarCollapsed: boolean
  onToggleSidebar: () => void
}) {
  const accounts = useDomainStore((s) => s.accounts)
  // 全舰队「偏离」计数（风险轴）：常驻顶栏、跟着翻页；随 5s 轮询自动增减，账户对回目标即清零。
  // 主动暂停的持仓偏离属于预期状态，不计入调仓提醒。
  const offCount = (accounts ?? []).filter(needsAdjustmentAttention).length
  const sidebarLabel = sidebarCollapsed ? '展开侧栏' : '收起侧栏'
  const SidebarIcon = sidebarCollapsed ? PanelLeftOpen : PanelLeftClose

  return (
    <header className="flex flex-none flex-wrap items-center gap-3 border-b border-line bg-surface px-5 py-3">
      <button
        ref={navigationTriggerRef}
        type="button"
        aria-label="打开主导航"
        className="text-ink-2 hover:text-ink-1 md:hidden"
        onClick={onOpenNavigation}
      >
        <Menu size={18} aria-hidden />
      </button>
      <Tooltip content={sidebarLabel}>
        <button type="button" aria-label={sidebarLabel} aria-controls="desktop-account-navigation" aria-expanded={!sidebarCollapsed}
          className="hidden size-7 shrink-0 cursor-pointer place-items-center rounded-md text-ink-2 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent md:grid"
          onClick={onToggleSidebar}>
          <SidebarIcon size={16} aria-hidden />
        </button>
      </Tooltip>
      <Link to="/" aria-label="axile 首页">
        <BrandWordmark />
      </Link>
      <ServiceConnectionStatus />
      {/* 偏离计数：风险从单页抬到全局。琥珀（偏离色）、仅 N>0 才现身（安静即好），点击回舰队。 */}
      {accounts != null && offCount > 0 && (
        <Link
          to="/"
          title={`${offCount} 个账户偏离目标 · 需要看看`}
          className="inline-flex items-center gap-1.5 rounded-chip border border-warn/45 bg-warn-tint px-2.5 py-1 text-[14px] font-medium text-warn hover:border-warn/70"
        >
          <TriangleAlert size={13} aria-hidden /> {offCount} 个偏离
        </Link>
      )}
      <span className="flex-1" />
      <ToolsButton />
      <TimeCheckButton />
      <ThemeToggle />
    </header>
  )
}
