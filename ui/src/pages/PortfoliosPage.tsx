import { useEffect, useState } from 'react'
import { useSearchParams, useViewTransitionState } from 'react-router'
import { Pencil, Plus, Trash2 } from 'lucide-react'
import { Link, useNavigate } from '@/components/ui/nav'
import { Card, Chip } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { Tooltip } from '@/components/ui/Tooltip'
import { ConfirmModal, type ConfirmSpec } from '@/components/ui/ConfirmModal'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { CreatePortfolioModal } from '@/features/portfolio/CreatePortfolioModal'
import { TargetChips } from '@/features/portfolio/TargetChips'
import { usePortfoliosTargets } from '@/features/portfolio/usePortfoliosTargets'
import { portfolioNameVtName } from '@/features/portfolio/viewTransition'
import {
  portfolioRollup,
  PORTFOLIO_ROLLUP_ICON,
  PORTFOLIO_ROLLUP_TEXT_CLASS,
} from '@/features/portfolio/portfolioRollup'
import { deletePortfolio } from '@/lib/api/portfolios'
import {
  formatTargetWeight,
  formatTargetUpdatedAt,
  portfolioTargetState,
  targetDirectionClass,
  type PortfolioTargetState,
} from '@/features/portfolio/portfolioCardSummary'
import { channelLabel } from '@/features/dashboard/display'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import type { AccountDashboardItem, PortfolioLite } from '@/types/api'

/** 组合列表 /portfolios —— 组合的「家」，保证每个组合始终可达 + 管理入口。 */
export function PortfoliosPage() {
  const navigate = useNavigate()
  const toast = useToastStore((s) => s.toast)
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null)
  const [deleteError, setDeleteError] = useState<Error | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)

  // 「新建组合」深链（侧栏 / 设置首页）：弹层打开后立即抹掉 ?new=1，不把弹层态留在历史里。
  useEffect(() => {
    if (searchParams.get('new') === '1') {
      setCreateOpen(true)
      setSearchParams({}, { replace: true })
    }
  }, [searchParams, setSearchParams])

  const portfolios = useDomainStore((s) => s.portfolios)
  const accounts = useDomainStore((s) => s.accounts)
  const portfoliosError = useDomainStore((s) => s.portfoliosError)
  const accountsError = useDomainStore((s) => s.accountsError)
  const refreshPortfolios = useDomainStore((s) => s.refreshPortfolios)

  const followersOf = (pid: number | null): AccountDashboardItem[] =>
    pid == null ? [] : (accounts ?? []).filter((a) => a.portfolio_id === pid)

  const askDelete = (p: PortfolioLite) => {
    if (p.id == null) return
    const followers = followersOf(p.id)
    if (followers.length > 0) {
      const names = followers.map((f) => f.name).join('、')
      setConfirm({
        title: '无法删除',
        body: `「${p.name}」正被账户 ${names} 绑定。需先在该账户改绑到其它组合、或解绑后，才能删除本组合。`,
        okText: `查看 ${followers[0].name}`,
        onConfirm: () => navigate(`/accounts/${followers[0].account_id}`),
      })
      return
    }
    setConfirm({
      title: '删除组合',
      body: `删除「${p.name}」。此操作不可撤销。`,
      okText: '删除',
      danger: true,
      onConfirm: async () => {
        setDeleteError(null)
        try {
          await deletePortfolio(p.id!)
          toast('组合已删除')
          void refreshPortfolios()
        } catch (e) {
          setDeleteError(e instanceof Error ? e : new Error(String(e)))
        }
      },
    })
  }

  const list = portfolios ?? []

  // 目标快照页面层统一拉取：卡片目标态与页头判词同一份派生结果，不各自为政。
  const targetFetches = usePortfoliosTargets(list.map((p) => p.id))
  const targetStates = list.map((p): PortfolioTargetState =>
    p.id == null
      ? { kind: 'uncalculated' }
      : portfolioTargetState(
          targetFetches[p.id]?.snapshot ?? null,
          targetFetches[p.id]?.loading ?? true,
          targetFetches[p.id]?.error ?? null,
          !!targetFetches[p.id]?.error,
        ),
  )
  const targetsReady = list.every((p) => p.id == null || (targetFetches[p.id] !== undefined && !targetFetches[p.id].loading))
  const unboundCount = accounts != null && !accountsError
    ? list.filter((p) => followersOf(p.id).length === 0).length
    : null
  const rollup = portfolioRollup({ total: list.length, targetStates, unboundCount, targetsReady })

  return (
    <section>
      {/* 判词与创建入口是一个左簇：按钮贴着状态句回答「要加一个在这里」，与账户舰队页摘要行同构。 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className={`inline-flex items-center gap-1.5 text-[19px] font-[640] ${PORTFOLIO_ROLLUP_TEXT_CLASS[rollup.key]}`}>
          {rollup.text === '组合' ? null : <span aria-hidden>{PORTFOLIO_ROLLUP_ICON[rollup.key]}</span>}
          {rollup.text}
        </span>
        <button
          type="button"
          onClick={() => setCreateOpen(true)}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-chip border border-line px-3 py-1.5 text-[13px] text-ink-2 transition-colors duration-150 hover:border-border-strong hover:text-ink-1 motion-reduce:transition-none"
        >
          <Plus size={14} aria-hidden />新建组合
        </button>
      </div>

      {portfolios == null && !portfoliosError && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(360px,1fr))] gap-4">
          {Array.from({ length: 2 }, (_, index) => (
            <Card key={index} className="border px-6 py-4" aria-busy="true">
              <div className="flex items-center gap-3">
                <Skeleton className="h-5 w-32" />
                <Skeleton className="h-5 w-16" />
                <Skeleton className="ml-auto h-8 w-16" />
              </div>
              <div className="mt-3 grid grid-cols-3 gap-3 border-t border-line pt-3">
                {Array.from({ length: 3 }, (_, metric) => <Skeleton key={metric} className="h-9 w-full" />)}
              </div>
              <Skeleton className="mt-3 h-10 w-full" />
              <div className="mt-3 border-t border-line pt-3"><Skeleton className="h-6 w-2/3" /></div>
            </Card>
          ))}
        </div>
      )}
      <ErrorNotice intent="read"
        title={portfolios == null ? '组合加载失败' : '组合更新失败'}
        error={portfoliosError} hasData={portfolios != null}
        variant={portfolios == null ? 'section' : 'stale'}
        onRetry={refreshPortfolios}
      />
      <ErrorNotice intent="mutation" title="删除组合失败" error={deleteError} variant="mutation" />

      {portfolios != null && list.length === 0 && (
        <Card className="p-8 text-center">
          <p className="text-[16px] font-[620]">还没有组合</p>
          <p className="mt-2 text-[15px] text-ink-2">组合是「交易什么」的来源，可被多个账户复用。</p>
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="mt-6 inline-flex cursor-pointer items-center gap-1.5 rounded-chip bg-accent px-4 py-2 text-[14px] font-medium text-white transition-colors duration-150 hover:brightness-110 motion-reduce:transition-none"
          >
            <Plus size={14} aria-hidden />新建组合
          </button>
        </Card>
      )}

      {list.length > 0 && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(360px,1fr))] gap-4">
          {list.map((p, index) => (
            <PortfolioCard key={p.id} p={p} targetState={targetStates[index]} targetFetch={p.id == null ? undefined : targetFetches[p.id]} followers={followersOf(p.id)} accountsReady={accounts != null} accountsError={accountsError} onDelete={askDelete} />
          ))}
        </div>
      )}

      <CreatePortfolioModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(created) => {
          setCreateOpen(false)
          if (created.id != null) navigate(`/portfolios/${created.id}/edit`)
        }}
      />
      <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />
    </section>
  )
}

/** 组合列表卡。抽成组件以便每卡独立用 useViewTransitionState 挂共享名（共享元素 FLIP）。目标态由页面层统一取数后传入。 */
function PortfolioCard({
  p,
  targetState,
  targetFetch,
  followers,
  accountsReady,
  accountsError,
  onDelete,
}: {
  p: PortfolioLite
  targetState: PortfolioTargetState
  targetFetch?: import('@/features/portfolio/usePortfoliosTargets').PortfolioTargetFetch
  followers: AccountDashboardItem[]
  accountsReady: boolean
  accountsError: Error | null
  onDelete: (p: PortfolioLite) => void
}) {
  const navigate = useNavigate()
  const to = `/portfolios/${p.id}/edit`
  // 仅「正在跳去本组合工作台」时给组合名挂共享名，与工作台标题槽配对做平移放大。
  const nameVt = useViewTransitionState(to)
  return (
    <Card
      className="h-full border px-6 py-4 transition-transform hover:-translate-y-px"
      onClick={() => navigate(to)}
    >
      <div className="flex min-w-0 items-center gap-3">
        <div
          className="min-w-0 truncate text-[16px] font-[620]"
          style={nameVt && p.id != null ? { viewTransitionName: portfolioNameVtName(p.id) } : undefined}
        >
          {p.name}
        </div>
        <Chip className="flex-none">{p.market}</Chip>
        <div className="ml-auto flex flex-none items-center gap-2" onClick={(e) => e.stopPropagation()}>
          <Tooltip content="编辑组合">
            <button
              type="button"
              aria-label="编辑组合"
              className="grid h-8 w-8 cursor-pointer place-items-center rounded-lg border border-line text-ink-2 hover:border-ink-3/40 hover:text-ink-1"
              onClick={() => navigate(to)}
            >
              <Pencil size={14} aria-hidden />
            </button>
          </Tooltip>
          <Tooltip content={accountsReady && !accountsError ? '删除组合' : '绑定关系确认后才能删除'}>
            <button
              type="button"
              aria-label="删除组合"
              className="grid h-8 w-8 cursor-pointer place-items-center rounded-lg border border-line text-ink-2 enabled:hover:border-warn/55 enabled:hover:bg-warn-soft enabled:hover:text-warn focus-visible:border-warn/55 focus-visible:bg-warn-soft focus-visible:text-warn disabled:cursor-not-allowed disabled:opacity-45"
              disabled={!accountsReady || Boolean(accountsError)}
              onClick={() => onDelete(p)}
            >
              <Trash2 size={14} aria-hidden />
            </button>
          </Tooltip>
        </div>
      </div>

      <TargetSummary state={targetState} />
      <div onClick={(event) => event.stopPropagation()}><ErrorNotice intent="read" title={`${p.name} 目标读取失败`} error={targetFetch?.error} hasData={targetFetch?.snapshot != null} evidence={[{ label: '组合 ID', value: String(p.id) }]} variant={targetFetch?.snapshot ? 'stale' : 'compact'} updatedAt={targetFetch?.updatedAt} onRetry={targetFetch?.refresh} contextKey={p.id ?? undefined} /></div>

      <div className="mt-3 flex min-w-0 flex-wrap items-center justify-between gap-3 border-t border-line pt-3 text-[13.5px] text-ink-3">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
          {accountsError ? (
            <span className="text-ink-3">绑定关系暂不可用</span>
          ) : !accountsReady ? (
            <Skeleton className="h-7 w-36" />
          ) : followers.length > 0 ? (
            followers.map((follower) => <PortfolioFollowerLink key={follower.account_id} account={follower} />)
          ) : (
            '未绑定账户'
          )}
        </div>
        <TargetTimestamp state={targetState} snapshot={targetFetch?.snapshot} />
      </div>
    </Card>
  )
}

function TargetSummary({ state }: { state: PortfolioTargetState }) {
  if (state.kind === 'loading') {
    return (
      <div className="mt-3 grid grid-cols-3 gap-5 border-t border-line pt-3" aria-busy="true" aria-label="正在读取组合目标">
        {Array.from({ length: 3 }, (_, index) => (
          <div key={index}><Skeleton className="h-3 w-14" /><Skeleton className="mt-2 h-4 w-20" /></div>
        ))}
        <div className="col-span-3 mt-1.5 flex gap-1.5" aria-hidden="true">
          {Array.from({ length: 4 }, (_, index) => (
            <Skeleton key={index} className="h-6 w-28" />
          ))}
        </div>
      </div>
    )
  }

  if (state.kind === 'unavailable' || state.kind === 'uncalculated' || state.kind === 'empty') {
    const text = state.kind === 'unavailable'
      ? '目标暂不可用'
      : state.kind === 'uncalculated'
        ? '尚无目标快照'
        : '目标为空仓'
    return (
      <div className="mt-3 flex min-h-[62px] items-center border-t border-line pt-3">
        <span className="text-[14px] text-ink-3">{text}</span>
      </div>
    )
  }

  const { summary } = state
  return (
    <div className="mt-3 border-t border-line pt-3">
      <div className="grid grid-cols-3 gap-3">
        <Metric label="目标品种" value={`${summary.activeCount} 个`} quiet />
        <Metric label="总敞口" value={formatTargetWeight(summary.grossExposure, false)} />
        <Metric label="净敞口" value={formatTargetWeight(summary.netExposure)} valueClass={targetDirectionClass(summary.netExposure)} />
      </div>
      <TargetChips entries={summary.entries} />
    </div>
  )
}

function Metric({
  label,
  value,
  valueClass = 'text-ink-1',
  quiet = false,
}: {
  label: string
  value: string
  valueClass?: string
  quiet?: boolean
}) {
  return (
    <div className="min-w-0">
      <div className="text-[12.5px] text-ink-3">{label}</div>
      <div className={`mt-0.5 truncate ${quiet ? 'text-[14.5px] font-[580]' : 'text-[16px] font-[640]'} ${valueClass}`}>{value}</div>
    </div>
  )
}

function TargetTimestamp({ state, snapshot }: {
  state: PortfolioTargetState
  snapshot?: import('@/types/api').TargetWeightSnapshot | null
}) {
  const accounts = useDomainStore((store) => store.accounts)
  const contextId = snapshot?.context_account_id
  const context = contextId == null ? '样例数据' : `账户 ${accounts?.find(account => account.account_id === contextId)?.name ?? `#${contextId}`}`
  if (state.kind !== 'ready' && state.kind !== 'empty') return null
  return (
    <span className="max-w-full break-words text-[12.5px] text-ink-3">
      {context} · 目标更新于{formatTargetUpdatedAt(state.calculatedAt)}
    </span>
  )
}

function PortfolioFollowerLink({ account }: { account: AccountDashboardItem }) {
  const to = `/accounts/${account.account_id}`
  const transitioning = useViewTransitionState(to)
  return (
    <Link
      to={to}
      className="-m-1 inline-flex min-w-0 items-center gap-2 rounded-md p-1 text-inherit hover:bg-fill focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      onClick={(event) => event.stopPropagation()}
      aria-label={`查看账户 ${account.name}，${channelLabel(account.trade_channel, account.market)}`}
    >
      <span
        className="max-w-32 truncate text-[15px] font-[620] text-ink-1"
        style={transitioning ? { viewTransitionName: `account-name-${account.account_id}` } : undefined}
      >
        {account.name}
      </span>
      <Chip style={transitioning ? { viewTransitionName: `account-channel-${account.account_id}` } : undefined}>
        {channelLabel(account.trade_channel, account.market)}
      </Chip>
    </Link>
  )
}
