import { useCallback } from 'react'
import { ChartNoAxesCombined, ChevronDown, History, Layers } from 'lucide-react'
import { BackLink } from '@/components/ui/BackLink'
import { Card, Chip } from '@/components/ui/Card'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Link } from '@/components/ui/nav'
import { ArchiveAction } from '@/features/account/ArchiveAction'
import { RuntimeSyncStatus } from '@/features/account/RuntimeSyncStatus'
import { useAccountRuntimeSync } from '@/features/account/useAccountRuntimeSync'
import { channelLabel } from '@/features/dashboard/display'
import { shanghaiLabel } from '@/features/history/costs'
import { describeCron } from '@/features/setup/cron'
import { getPortfolio } from '@/lib/api/portfolios'
import { usePolling } from '@/lib/hooks/usePolling'
import { useChannelDescriptor } from '@/stores/channels'
import type { Account } from '@/types/api'

/** 归档详情只读保存的配置；不挂载会刷新柜台资产的运行面板。 */
export function ArchivedAccountDetail({ account, onChanged }: {
  account: Account
  onChanged: () => void | Promise<void>
}) {
  const runtime = useAccountRuntimeSync(account.id!)
  const channel = useChannelDescriptor(account.trade_channel)
  const cron = account.cron_expr.trim()
  const schedule = cron && channel?.schedule
    ? describeCron(channel.schedule.kind, cron, channel.schedule.night, account.gm_sessions)
    : null
  const portfolio = usePolling(useCallback((signal: AbortSignal) => getPortfolio(account.portfolio_id!, signal), [account.portfolio_id]), {
    queryKey: `archived-account:${account.id}:portfolio:${account.portfolio_id}`,
    intervalMs: 0,
    enabled: account.portfolio_id != null,
  })
  return <section className="mx-auto w-full max-w-4xl space-y-5">
    <header className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-4 gap-y-1">
      <div className="flex min-w-0 items-start gap-2">
        <BackLink to="/?archived=1" label="返回已归档账户" />
        <div className="min-w-0 pt-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h1 className="min-w-0 text-[20px] font-[640] leading-tight [overflow-wrap:anywhere]">{account.name}</h1>
            <Chip className="shrink-0">已归档</Chip>
          </div>
        </div>
      </div>
      <div className="shrink-0 pt-1">
        <ArchiveAction kind="account" id={account.id!} name={account.name} archivedAt={account.archived_at} onChanged={onChanged} />
      </div>
      <p className="col-span-2 ml-[52px] text-[13px] text-ink-2 md:ml-11">归档于 <time dateTime={account.archived_at!}>{shanghaiLabel(account.archived_at!)}</time></p>
    </header>
    <Card>
      <div className="px-5 py-5 sm:px-6 sm:py-6">
        <dl className="space-y-5 text-[14px] leading-6">
          <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-x-4 sm:grid-cols-[6rem_minmax(0,1fr)]">
            <dt className="text-ink-2">交易渠道</dt>
            <dd className="min-w-0 [overflow-wrap:anywhere]">{channelLabel(account.trade_channel, account.market)}
              {account.brokerage && account.brokerage !== account.trade_channel && <span className="ml-3 text-ink-2">{account.brokerage}</span>}
            </dd>
          </div>
          <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-x-4 sm:grid-cols-[6rem_minmax(0,1fr)]">
            <dt className="text-ink-2">绑定组合</dt>
            <dd className="min-w-0 [overflow-wrap:anywhere]">{portfolio.data
              ? <><Link className="text-ink-1 underline decoration-line underline-offset-4 hover:text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" to={`/portfolios/${portfolio.data.id}/edit`}>{portfolio.data.name}</Link>
                {portfolio.data.archived_at && <span className="ml-2 text-xs text-ink-2">已归档</span>}</>
              : account.portfolio_id == null ? <span className="text-ink-2">未绑定</span> : `#${account.portfolio_id}`}</dd>
          </div>
          <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-x-4 sm:grid-cols-[6rem_minmax(0,1fr)]">
            <dt className="text-ink-2">执行节奏</dt>
            <dd className="min-w-0">
              <span>{cron ? schedule ?? '自定义执行节奏' : '未设置自动执行'}</span>
              <span className="ml-2 text-xs text-ink-2">已暂停</span>
              {cron && <details className="group mt-1.5">
                <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-xs text-ink-2 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent [&::-webkit-details-marker]:hidden">
                  查看原始规则<ChevronDown size={12} className="transition-transform group-open:rotate-180 motion-reduce:transition-none" aria-hidden />
                </summary>
                <pre className="mt-2 whitespace-pre-wrap rounded-lg bg-fill px-3 py-2 font-mono text-xs leading-relaxed text-ink-2 [overflow-wrap:anywhere]">{cron.replace(/\s*\|\s*/g, '\n')}</pre>
              </details>}
            </dd>
          </div>
          {account.remark && <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-x-4 sm:grid-cols-[6rem_minmax(0,1fr)]">
            <dt className="text-ink-2">备注</dt><dd className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{account.remark}</dd>
          </div>}
        </dl>
        <ErrorNotice intent="read" title="绑定组合读取失败" error={portfolio.error} />
      </div>
      <nav aria-label="归档账户历史" className="flex flex-wrap gap-x-2 gap-y-1 border-t border-line px-3 py-2.5 sm:px-4">
        <Link className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-ink-2 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" to={`/accounts/${account.id}/executions`}><History size={16} aria-hidden />执行记录与回放</Link>
        <Link className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-ink-2 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" to={`/accounts/${account.id}/history`}><ChartNoAxesCombined size={16} aria-hidden />历史绩效</Link>
        <Link className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-ink-2 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" to={`/accounts/${account.id}/holdings`}><Layers size={16} aria-hidden />保存的持仓</Link>
      </nav>
    </Card>
    <p className="px-1 text-xs leading-relaxed text-ink-2">归档不会平仓或撤单，历史快照不代表实时持仓。恢复后仍保持暂停。</p>
    <RuntimeSyncStatus runtime={runtime} accountId={account.id!} />
  </section>
}
