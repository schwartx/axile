import { ArrowLeftRight, RefreshCw } from 'lucide-react'
import type { ReactNode } from 'react'
import { BackLink } from '@/components/ui/BackLink'
import { Chip } from '@/components/ui/Card'
import { Link } from '@/components/ui/nav'
import { NumberTicker } from '@/components/ui/NumberTicker'
import { OverflowText } from '@/components/ui/OverflowText'
import { Tooltip } from '@/components/ui/Tooltip'
import { AssetObservationTime } from '@/features/account/AssetObservationTime'
import { CashFlowPanel } from '@/features/account/CashFlowPanel'
import { channelLabel } from '@/features/dashboard/display'
import { displayCurrencyUnit } from '@/lib/format'
import type { AccountDashboardItem } from '@/types/api'

export function AccountOverviewIdentity({ item, nameTransition }: {
  item: AccountDashboardItem
  nameTransition: boolean
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-3">
      <BackLink to="/" label="返回所有账户" />
      <span className="text-[17px] font-[620]"
        style={nameTransition ? { viewTransitionName: `account-name-${item.account_id}` } : undefined}>
        {item.name}
      </span>
      <Chip style={nameTransition ? { viewTransitionName: `account-channel-${item.account_id}` } : undefined}>
        {channelLabel(item.trade_channel, item.market)}
      </Chip>
    </div>
  )
}

export function AccountOverviewPortfolio({ portfolioId, portfolioName, editTo, showBindAction = false }: {
  portfolioId: number | null
  portfolioName: string | null
  editTo: string
  showBindAction?: boolean
}) {
  const action = portfolioId != null ? '更换组合' : '绑定组合'
  return (
    <div className="mt-1.5 text-[14.5px] text-ink-2">
      {'组合 '}
      {portfolioId != null ? (
        <Link to={`/portfolios/${portfolioId}/edit`}
          className="inline-flex min-w-0 max-w-60 align-middle font-semibold text-accent hover:underline">
          <OverflowText className="min-w-0" text={portfolioName ?? `#${portfolioId}`} />
        </Link>
      ) : showBindAction ? (
        <Link to={editTo} className="align-middle font-semibold text-accent hover:underline">{action}</Link>
      ) : (
        <span className="align-middle text-ink-3">未绑定</span>
      )}
      {(portfolioId != null || !showBindAction) && (
        <Tooltip content={action}>
          <span className="ml-0.5 inline-flex align-middle">
            <Link to={editTo} aria-label={action}
              className="grid h-5 w-5 place-items-center rounded-md text-ink-3 hover:bg-fill hover:text-ink-1">
              <ArrowLeftRight size={13} aria-hidden />
            </Link>
          </span>
        </Tooltip>
      )}
    </div>
  )
}

export function AccountOverviewEquity({
  accountId, equity, currency, observedAt, summary, refreshing, refreshDisabledReason, onRefresh, feedback,
}: {
  accountId: number
  equity: number | null
  currency: string
  observedAt?: string | null
  summary?: ReactNode
  refreshing: boolean
  refreshDisabledReason?: string
  onRefresh: () => void
  feedback?: ReactNode
}) {
  const refreshLabel = refreshDisabledReason ?? '刷新账户资产'
  return (
    <div className="min-w-0 max-w-full shrink-0 [--number-flow-mask-height:0.08em]">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <div className="num whitespace-nowrap text-[35px] leading-none font-[640] tracking-tight">
          {equity == null ? '—' : <NumberTicker value={equity} format={{ minimumFractionDigits: 2, maximumFractionDigits: 2 }} />}
          <span className="ml-1.5 text-[17px] font-medium text-ink-3">{displayCurrencyUnit(currency)}</span>
        </div>
        {summary}
        <CashFlowPanel key={accountId} accountId={accountId} />
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-ink-3">
        <span><AssetObservationTime observedAt={observedAt} /></span>
        <Tooltip content={refreshLabel}>
          <span className="inline-flex">
            <button type="button" onClick={onRefresh} disabled={refreshing || !!refreshDisabledReason}
              aria-label={refreshing ? '正在查询账户资产' : '刷新账户资产'} title={refreshLabel}
              className="grid h-6 w-6 cursor-pointer place-items-center rounded-md text-ink-3 hover:bg-fill hover:text-ink-1 disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent">
              <RefreshCw size={14} className={refreshing ? 'animate-spin motion-reduce:animate-none' : undefined} />
            </button>
          </span>
        </Tooltip>
        {feedback}
      </div>
    </div>
  )
}
