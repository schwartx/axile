import { useState } from 'react'
import { useViewTransitionState } from 'react-router'
import { Copy, Plus } from 'lucide-react'
import { CopyAccountDialog } from '@/features/account/CopyAccountDialog'
import { cardPerformance, currentEquity } from '@/features/dashboard/performance'
import { Link, useNavigate } from '@/components/ui/nav'
import { Card } from '@/components/ui/Card'
import { NumberTicker } from '@/components/ui/NumberTicker'
import { Skeleton } from '@/components/ui/Skeleton'
import { PerformanceCurveLink } from '@/features/history/PerformanceCurveLink'
import { ExposureBar } from '@/components/viz/ExposureBar'
import { INTEGRITY_ICON, INTEGRITY_TEXT_CLASS, INTEGRITY_ORDER, channelLabel } from '@/features/dashboard/display'
import { isExecutingStatus, phaseLabel, runVerb } from '@/features/dashboard/execProgress'
import { integrityOf, needsAdjustmentAttention, gateOf, stateVerdict, holdingText, type Integrity } from '@/lib/derive'
import { displayCurrencyUnit } from '@/lib/format'
import { useRunning } from '@/stores/liveExec'
import type { AccountDashboardItem } from '@/types/api'

function FleetCard({
  item,
  portfolioName,
}: {
  item: AccountDashboardItem
  portfolioName: string | null
}) {
  const navigate = useNavigate()
  const [copyOpen, setCopyOpen] = useState(false)
  const to = `/accounts/${item.account_id}`
  // 仅「正在跳转到本卡」时给账户名挂共享名，与详情页头配对做共享元素 FLIP（平移 + 微缩）。
  const transitioning = useViewTransitionState(to)
  const state = stateVerdict(item)
  const gate = gateOf(item)
  // 服务端真源的在途执行：在跑时状态文案与卡边优先反映「正在执行」。
  const live = useRunning(item.account_id)
  // 当前权益来自资产查询；有前收时日涨跌 = 当前权益 / 前收 - 1。
  const equity = currentEquity(item)
  const { pct, dayLabel, statusLabel } = cardPerformance(item.performance, Date.now(), {
    equity,
    observedAt: item.asset_observed_at,
    previousClose: item.previous_close,
  })
  // 红涨绿跌：涨→up(红)、跌→down(绿)。
  const pctCls = pct == null ? 'text-ink-2' : pct > 0 ? 'text-up' : pct < 0 ? 'text-down' : 'text-ink-2'

  // 卡边：在跑走 accent（run 档，不碰红绿）；否则注意/失败同为琥珀，失败以更重边框强度区分。
  const borderCls = live
    ? 'border-accent/45 bg-accent-soft/40'
    : needsAdjustmentAttention(item)
      ? 'border-warn/45 bg-warn-tint'
      : 'border-transparent'

  return <>
    <Card
      className={`mb-4 border px-6 py-4 transition-transform hover:-translate-y-px ${borderCls}`}
      onClick={() => navigate(to)}
    >
      <div className="flex flex-wrap items-center gap-3">
        <span
          className="text-[16px] font-[620]"
          style={transitioning ? { viewTransitionName: `account-name-${item.account_id}` } : undefined}
        >
          {item.name}
        </span>
        {/* 渠道徽章与账户名同一逻辑单元：同门控挂名、同轨飞行，不与名字撕开。 */}
        <span
          className="rounded-chip bg-fill px-2 py-0.5 text-[12px] text-ink-2"
          style={transitioning ? { viewTransitionName: `account-channel-${item.account_id}` } : undefined}
        >
          {channelLabel(item.trade_channel, '')}
        </span>
        {gate.gate === 'paused' && (
          <span className="rounded-chip bg-fill px-2 py-0.5 text-[12px] text-ink-3">{gate.label}</span>
        )}
        <span className="text-xs text-ink-3">{portfolioName ?? (item.portfolio_id ? `组合 #${item.portfolio_id}` : '未绑定组合')}</span>
        {live ? (
          <span className="ml-auto inline-flex items-center gap-1.5 text-[14.5px] font-semibold text-accent">
            {isExecutingStatus(live.status)
              ? `⟳ 正在${runVerb(live.kind)} · ${phaseLabel(live.phase)}${live.pendingExecutionId ? ' · 结束后再调一次' : ''}`
              : '等待执行'}
          </span>
        ) : state.text !== gate.label ? (
          <span className={`ml-auto inline-flex items-center gap-1.5 text-[14.5px] font-semibold ${gate.gate === 'paused' ? 'text-ink-3' : INTEGRITY_TEXT_CLASS[state.integrity]}`}>
            {gate.gate !== 'paused' && INTEGRITY_ICON[state.integrity]} {state.text}
          </span>
        ) : <span className="ml-auto" />}
        <button
          type="button"
          className="inline-flex flex-none items-center gap-1 rounded-chip border border-line px-2.5 py-1 text-[13px] text-ink-2 hover:border-border-strong hover:text-ink-1"
          aria-label={`复制账户 ${item.name}`}
          onClick={(event) => { event.stopPropagation(); setCopyOpen(true) }}
        >
          <Copy size={13} aria-hidden />复制
        </button>
      </div>

      <div className="mt-3 flex items-end justify-between gap-3">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="num text-[27px] font-[640] tracking-tight">
            {equity == null ? '—' : <NumberTicker value={equity} format={{ minimumFractionDigits: 2, maximumFractionDigits: 2 }} />}
          </span>
          <span className="text-[14px] text-ink-3">{displayCurrencyUnit(item.currency)}</span>
          {pct == null && <span className="text-ink-3">{dayLabel} —</span>}
          <span className="text-xs text-ink-3">{statusLabel}</span>
          {pct != null && (
            <span className={`num text-[14px] ${pctCls}`}>
              {dayLabel} {pct >= 0 ? '+' : '−'}
              <NumberTicker value={Math.abs(pct)} format={{ minimumFractionDigits: 2, maximumFractionDigits: 2 }} suffix="%" />
            </span>
          )}
        </div>
        <PerformanceCurveLink accountId={item.account_id} summary={item.performance} source="fleet" width={120} height={34} />
      </div>

      <div className="mt-1 text-xs text-ink-3">{item.asset_observed_at ? `资产查询于 ${item.asset_observed_at}` : '尚未查询账户资产'}</div>
      <div className="mt-3 flex gap-2.5 border-t border-line pt-3 text-[14.5px]">
        <span className="min-w-14 flex-none text-ink-3">当前持仓</span>
        <span>{item.asset_observed_at ? holdingText(item.holdings_count, item.position_weights) : '持仓尚未查询'}</span>
      </div>
      <ExposureBar weights={item.position_weights} />
    </Card>
    <CopyAccountDialog accountId={item.account_id} name={item.name} isStarted={item.is_started} isBusy={Boolean(live)} open={copyOpen} onClose={() => setCopyOpen(false)} />
  </>
}

/** 舰队总览（所有账户的着陆列表）：汇总条 + 按状态排序的账户卡。 */
export function FleetView({
  items,
  portfolioNames,
}: {
  items: AccountDashboardItem[]
  portfolioNames: Map<number, string>
}) {
  const withIntegrity = items.map((item) => ({ item, integrity: integrityOf(item).integrity }))
  const pausedCount = items.filter((item) => !item.is_started).length
  const offCount = items.filter(needsAdjustmentAttention).length
  const unknownCount = withIntegrity.filter((x) => x.item.is_started && x.integrity === 'unknown').length
  // 自动账户偏离才提醒；暂停数量作为中性上下文，避免暗示全部到位。
  const rollup: { key: Integrity; text: string } =
    offCount > 0
      ? { key: 'off', text: `${items.length} 个账户 · ${offCount} 个需要看看` }
      : unknownCount > 0
        ? { key: 'unknown', text: `${items.length} 个账户 · ${unknownCount} 个待对账` }
        : pausedCount > 0
          ? { key: 'unknown', text: `${items.length} 个账户 · ${pausedCount} 个已暂停` }
          : { key: 'aligned', text: `${items.length} 个账户 · 全部到位 · 系统正常` }

  const sorted = [...withIntegrity].sort((a, b) => (a.item.is_started ? INTEGRITY_ORDER[a.integrity] : 3) - (b.item.is_started ? INTEGRITY_ORDER[b.integrity] : 3))

  return (
    <section>
      {/* 摘要与创建入口是一个左簇：按钮贴着状态句回答「要加一个在这里」，不甩到宽屏右缘。 */}
      <div className="mx-0.5 my-2 mb-6 flex flex-wrap items-center gap-3">
        <span className={`inline-flex items-center gap-1.5 text-[17px] font-[640] ${INTEGRITY_TEXT_CLASS[rollup.key]}`}>
          {rollup.key !== 'unknown' && INTEGRITY_ICON[rollup.key]} {rollup.text}
        </span>
        <Link
          to="/setup/acct/channel"
          className="inline-flex items-center gap-1.5 rounded-chip border border-line px-3 py-1.5 text-[13px] text-ink-2 transition-colors duration-150 hover:border-border-strong hover:text-ink-1 motion-reduce:transition-none"
        >
          <Plus size={14} aria-hidden />新建账户
        </Link>
      </div>
      {sorted.map(({ item }) => (
        <FleetCard
          key={item.account_id}
          item={item}
          portfolioName={item.portfolio_id != null ? (portfolioNames.get(item.portfolio_id) ?? null) : null}
        />
      ))}
    </section>
  )
}

/** 舰队冷拉骨架：汇总行 + 两张与成品同形的账户卡灰块。 */
export function FleetSkeleton() {
  return (
    <section aria-busy="true" aria-label="账户列表加载中">
      <Skeleton className="mx-0.5 my-2 mb-6 h-6 w-64" />
      {[0, 1].map((i) => (
        <div key={i} className="mb-4 rounded-card border border-line bg-surface px-6 py-4">
          <div className="flex items-center gap-3">
            <Skeleton className="h-5 w-28" />
            <Skeleton className="h-5 w-14" />
          </div>
          <div className="mt-3 flex items-end justify-between gap-3">
            <Skeleton className="h-8 w-44" />
            <Skeleton className="h-[34px] w-[120px]" />
          </div>
          <Skeleton className="mt-3 h-4 w-full" />
        </div>
      ))}
    </section>
  )
}
