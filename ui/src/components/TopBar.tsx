import { useEffect, useState, type RefObject } from 'react'
import { Link } from '@/components/ui/nav'
import { useDomainStore } from '@/stores/domain'
import { needsAdjustmentAttention } from '@/lib/derive'
import { ThemeToggle } from '@/components/ThemeToggle'
import { timeAgo } from '@/lib/format'
import { Menu, RefreshCw, TriangleAlert } from 'lucide-react'
import { Tooltip } from '@/components/ui/Tooltip'
import { shortErrorReason } from '@/lib/errorInfo'
import { BrandWordmark } from '@/components/brand/BrandWordmark'

/**
 * 顶栏 —— 品牌、后端活性点、风险提示与主题切换。
 *
 * 活性点/新鲜度直接来自共享账户数据的刷新（比单独 ping health 更诚实）：
 * 有数据且最近一次刷新无误=中性+「数据 N 秒前」；刷新出错=琥珀并保留原因。
 */
export function TopBar({
  navigationTriggerRef,
  onOpenNavigation,
}: {
  navigationTriggerRef?: RefObject<HTMLButtonElement | null>
  onOpenNavigation?: () => void
}) {
  const updatedAt = useDomainStore((s) => s.accountsUpdatedAt)
  const error = useDomainStore((s) => s.accountsError)
  const accounts = useDomainStore((s) => s.accounts)
  const refreshAccounts = useDomainStore((s) => s.refreshAccounts)
  const connecting = accounts == null && error == null
  const online = accounts != null && error == null && updatedAt != null
  // 全舰队「偏离」计数（风险轴）：常驻顶栏、跟着翻页；随 5s 轮询自动增减，账户对回目标即清零。
  // 主动暂停的持仓偏离属于预期状态，不计入调仓提醒。
  const offCount = (accounts ?? []).filter(needsAdjustmentAttention).length

  // 让「N 秒前」随时间走动，每秒重算一次。
  const [, force] = useState(0)
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const statusText = connecting
    ? '正在连接'
    : online
    ? `数据 ${timeAgo(updatedAt)} · 服务正常`
    : `与服务器失联 · ${shortErrorReason(error)}`

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
      <Link to="/" aria-label="axile 首页">
        <BrandWordmark />
      </Link>
      <span className="flex items-center gap-1.5 text-[14px] text-ink-2">
        {/* 心跳灯（离开红绿，红绿专供行情涨跌）：通电=信号青常亮（亮而不动，安静即好），
            连接中=青点搏动，失联=琥珀点。全站的「系统活着」都循同一灯语。 */}
        <span
          className={`h-2 w-2 flex-none rounded-full ${
            connecting
              ? 'animate-pulse bg-accent shadow-[0_0_0_3px_var(--color-accent-soft)] motion-reduce:animate-none'
              : online
              ? 'bg-accent shadow-[0_0_0_3px_var(--color-accent-soft)]'
              : 'bg-warn shadow-[0_0_0_3px_var(--color-warn-soft)]'
          }`}
        />
        {statusText}
      </span>
      {error && (
        <Tooltip content={`${shortErrorReason(error, 160)}；点击重试`}>
          <button
            type="button"
            aria-label="重试连接服务器"
            className="cursor-pointer text-ink-3 hover:text-warn"
            onClick={() => void refreshAccounts()}
          >
            <RefreshCw size={14} aria-hidden />
          </button>
        </Tooltip>
      )}
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
      <ThemeToggle />
    </header>
  )
}
