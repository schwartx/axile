import { displayCurrencyUnit } from '@/lib/format'
import { useCallback, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { Select } from '@/components/ui/Select'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { usePolling } from '@/lib/hooks/usePolling'
import { getActivitySymbols } from '@/lib/api/accounts'
import { getPerformanceCosts, type CostPage, type CostQuery } from '@/lib/api/performance'
import { getExecutionArtifacts, getExecutionEvents } from '@/lib/api/executions'
import { ExecutionErrorEvidence } from '@/features/history/ExecutionErrorEvidence'
import { readEvidencePages } from '@/features/history/executionErrorAdapter'
import { ExecutionEvidence } from '@/features/history/ExecutionEvidence'
import { quantityUnit } from '@/features/history/executionEvidenceModel'
import { buildExecutionDetail, type ExecutionDetailModel } from '@/features/account/executionDetail'
import { amount, quantityText, shanghaiLabel, lossClass, coverageText, durationText, type CostSummary, type CostTrade } from '@/features/history/costs'
import type { JournalExecution, JournalSymbol } from '@/features/account/executionJournal'
import { tradesFromArtifacts, type LiveWindow } from '@/features/account/journalActivity'
import type { SnapshotScope } from '@/features/account/journalSource'
import type { ChannelCapability } from '@/types/api'

export interface Expansion { open: boolean; count: number; side: string; cursors?: (string | undefined)[]; evidenceView?: 'actions' | 'positions' }
export type { LiveWindow }
const tableClass = 'w-full whitespace-nowrap text-left text-xs [&_td]:px-3 [&_td]:py-2 [&_th]:px-3 [&_th]:py-2'
export const EXEC_COLS = 'grid grid-cols-2 gap-x-4 gap-y-2 xl:grid-cols-[155px_minmax(120px,1fr)_70px_95px_105px_115px_55px]'
export const SYMBOL_COLS = 'grid grid-cols-2 gap-x-4 gap-y-2 xl:grid-cols-[minmax(120px,1fr)_110px_115px_120px_80px_145px_20px]'
const SIDES = [{ value: 'all', label: '全部方向' }, { value: 'buy', label: '买入' }, { value: 'sell', label: '卖出' }]
type Units = ChannelCapability['units']
type DetailLink = (id: string | null) => ReactNode

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <span className="block min-w-0 break-words xl:text-right"><span className="mr-2 text-xs text-ink-3 xl:hidden">{label}</span>{children}</span>
}
export function Slip({ summary }: { summary: CostSummary | null }) {
  return <span className={`num ${lossClass(summary?.lossBp ?? null)}`}>{amount(summary?.lossBp ?? null)}</span>
}
export function Cost({ summary }: { summary: CostSummary | null }) {
  return <span className="inline-flex flex-col items-start xl:items-end"><span className={`num ${lossClass(summary?.cost ?? null)}`}>{amount(summary?.cost ?? null)}</span>
    {summary != null && summary.count > 0 && (summary.covered < summary.count || !summary.amountComplete) && <span className="text-[11px] text-ink-3">已知成本 · {coverageText(summary)}</span>}</span>
}

// Immutable snapshot pages and terminal execution evidence survive collapse and detail navigation.
const tradePages = new Map<string, CostPage<CostTrade>>()
function remember<K, V>(cache: Map<K, V>, key: K, value: V) {
  cache.set(key, value)
  if (cache.size > 200) cache.delete(cache.keys().next().value!)
}
function readEvidence(row: JournalExecution, signal: AbortSignal): Promise<{ model: ExecutionDetailModel; trades: CostTrade[]; eventsError: Error | null; artifactsError: Error | null }> {
  const id = row.executionId!
  const flight = Promise.all([readEvidencePages((skip, limit) => getExecutionEvents(id, signal, { skip, limit }), signal), readEvidencePages((skip, limit) => getExecutionArtifacts(id, signal, { skip, limit }), signal)])
    .then(([events, artifacts]) => ({
      model: buildExecutionDetail(events.data, artifacts.data),
      eventsError: events.error, artifactsError: artifacts.error,
      trades: tradesFromArtifacts(artifacts.data, { id: row.recordId, execution_id: id, created_at: row.time }),
    }))
  return flight
}

export function TradeTable({ trades, currency, units, detailLink }: { trades: CostTrade[]; currency: string; units?: Units; detailLink?: DetailLink }) {
  return <div className="relative overflow-x-auto"><table className={tableClass}><thead className="text-ink-3"><tr><th>成交时间</th><th>品种</th><th>方向 / 数量</th><th>参考价</th><th>成交价</th><th>成交额 {displayCurrencyUnit(currency)}</th><th>滑点损耗 BP</th><th>滑点成本 {displayCurrencyUnit(currency)}</th>{detailLink && <th>详情</th>}</tr></thead><tbody>
    {trades.map((trade, i) => <tr key={trade.trade_id ?? `${trade.record_id}:${trade.symbol}:${i}`} className="border-t border-line"><td>{shanghaiLabel(new Date(trade.time).toISOString())}{trade.timeEstimated && <small className="block text-warn">使用执行时间</small>}</td><td>{trade.symbol}</td><td>{trade.side === 'buy' ? '买' : trade.side === 'sell' ? '卖' : '方向未知'} / {quantityText(trade.quantity)} {quantityUnit(units, trade.symbol, currency)}</td><td>{amount(trade.reference)}{trade.referenceSource === 'last' && <small className="block text-ink-3">到达最新价</small>}</td><td>{amount(trade.price)}</td><td>{amount(trade.value)}</td><td className={lossClass(trade.lossBp)}>{amount(trade.lossBp)}</td><td className={lossClass(trade.cost)}>{amount(trade.cost)}</td>{detailLink && <td>{detailLink(trade.execution_id ?? null)}</td>}</tr>)}
  </tbody></table></div>
}

function TradeList({ accountId, scope, trades, expansion, update, currency, units, detailLink, scoped, loading }: {
  accountId: number; scope?: SnapshotScope; trades: CostTrade[]; expansion: Expansion; update: (next: Expansion) => void
  currency: string; units?: Units; detailLink?: DetailLink; scoped: boolean; loading?: boolean
}) {
  const cursors = expansion.cursors ?? [undefined]
  const side = expansion.side === 'buy' || expansion.side === 'sell' ? expansion.side : undefined
  const query: CostQuery | null = scope ? { ...scope, dimension: 'trade', side, cursor: cursors.at(-1) } : null
  const key = JSON.stringify(query)
  const poll = usePolling(useCallback(async (signal: AbortSignal) => {
    const cacheKey = `${accountId}:${key}`
    const cached = tradePages.get(cacheKey)
    if (cached) return cached
    const page = await getPerformanceCosts<CostTrade>(accountId, JSON.parse(key), signal)
    if (!signal.aborted) remember(tradePages, cacheKey, page)
    return page
  }, [accountId, key]), { queryKey: `journal-trades:${accountId}:${key}`, enabled: !!scope && expansion.open, intervalMs: 0 })
  const local = trades.filter(t => !side || t.side === side).toSorted((a, b) => b.time - a.time)
  const shown = scope ? poll.data?.data ?? [] : local.slice(0, expansion.count)
  const count = scope ? poll.data?.count ?? 0 : local.length
  const pending = scope ? poll.loading : !!loading
  return <div className="py-3" data-testid="journal-trades">
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-ink-3"><span>{scoped ? '区间内成交' : '逐笔成交'} · {(scope && !poll.data) || pending ? '—' : count} 笔</span>
      <Select ariaLabel="成交方向" value={expansion.side} options={SIDES} onChange={side => update({ ...expansion, side, count: 20, cursors: [undefined] })} /></div>
    <ErrorNotice intent="read" title="成交读取失败" hasData={poll.data != null} error={poll.error} onRetry={poll.refresh} />
    {pending && <p role="status" className="py-3 text-xs text-ink-3">成交读取中</p>}
    <TradeTable trades={shown} currency={currency} units={units} detailLink={detailLink} />
    {!pending && !poll.error && count === 0 && <p className="py-3 text-xs text-ink-3">无匹配成交</p>}
    {scope && poll.data ? <div className="flex min-h-9 items-center justify-end gap-4 text-xs text-ink-3"><span>{count} 笔 · 第 {cursors.length} 页</span><button className="min-h-9 text-accent disabled:opacity-30" disabled={cursors.length === 1} onClick={() => update({ ...expansion, cursors: cursors.slice(0, -1) })}>上一页</button><button className="min-h-9 text-accent disabled:opacity-30" disabled={!poll.data.next_cursor} onClick={() => update({ ...expansion, cursors: [...cursors, poll.data!.next_cursor!] })}>下一页</button></div>
      : !scope && count > shown.length && <button className="min-h-9 text-xs text-accent" onClick={() => update({ ...expansion, count: expansion.count + 20 })}>加载更多成交</button>}
  </div>
}

function ExecutionExpansion({ row, accountId, scope, expansion, update, currency, units, detailLink }: RowProps) {
  const evidence = usePolling(useCallback((signal: AbortSignal) => readEvidence(row, signal), [row]), {
    queryKey: `journal-evidence:${row.executionId}`, intervalMs: 0, enabled: expansion.open && !!row.executionId,
  })
  const scoped = !!scope && (scope.start != null || !!scope.day)
  return <div className="border-l-2 border-accent px-3 py-2" data-testid="execution-expansion">
    <div className="mb-2 flex min-h-9 flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      {detailLink(row.executionId)}
      <span aria-hidden className="text-ink-3">·</span>
      <ExecutionErrorEvidence accountId={accountId} executionId={row.executionId} recordId={row.recordId} record={row.record} enabled={expansion.open} className="inline-flex items-center [&>button]:text-xs" />
      {scope && <span className="text-ink-3">完整执行持仓变化</span>}
    </div>
    {!row.executionId && <p className="py-2 text-xs text-ink-3">历史记录无执行附件</p>}
    <ErrorNotice intent="read" title="执行证据读取失败" hasData={evidence.data != null} error={evidence.error} onRetry={evidence.refresh} />
    <ErrorNotice intent="read" title="执行事件读取失败" error={evidence.data?.eventsError} onRetry={evidence.refresh} />
    <ErrorNotice intent="read" title="执行附件读取失败" error={evidence.data?.artifactsError} onRetry={evidence.refresh} />
    {evidence.loading && <p role="status" className="py-2 text-xs text-ink-3">执行证据读取中</p>}
    {evidence.data && <ExecutionEvidence model={evidence.data.model} currency={currency} units={units} view={expansion.evidenceView ?? 'actions'} onViewChange={evidenceView => update({ ...expansion, evidenceView })} />}
    <TradeList accountId={accountId} scope={scope && row.recordId != null ? { ...scope, record_id: row.recordId } : undefined} trades={scope ? row.trades : evidence.data?.trades ?? []} expansion={expansion} update={update} currency={currency} units={units} scoped={scoped} loading={!scope && evidence.loading} />
  </div>
}
interface RowProps { row: JournalExecution; accountId: number; scope?: SnapshotScope; expansion: Expansion; update: (next: Expansion) => void; currency: string; units?: Units; detailLink: DetailLink; liveWindow?: LiveWindow }
export function ExecutionGroup(props: RowProps) {
  const { row, expansion, update, scope } = props
  const panel = `execution-panel-${row.key}`
  const expandable = row.recordId != null || !!row.executionId || row.trades.length > 0
  return <div className="border-b border-line" data-journal-record={row.recordId ?? undefined}>
    <button type="button" disabled={!expandable} aria-expanded={expandable ? expansion.open : undefined} aria-controls={expandable ? panel : undefined} aria-label={`${expansion.open ? '收起' : '展开'}执行 ${row.recordId ?? row.key}`} onClick={() => update({ ...expansion, open: !expansion.open })} className={`${EXEC_COLS} w-full items-baseline px-3 py-4 text-left text-[13px] hover:bg-bg-subtle disabled:cursor-default ${expansion.open ? 'bg-bg-subtle' : ''}`}>
      <span className="flex min-w-0 items-center gap-1 num text-xs text-ink-3">{expandable && <ChevronDown size={14} className={`shrink-0 transition-transform motion-reduce:transition-none ${expansion.open ? 'rotate-180' : ''}`} />}{shanghaiLabel(row.time)}</span>
      <span className="col-span-2 min-w-0 break-words text-ink-2 xl:col-span-1">{row.description}{scope && (scope.start != null || scope.day) && <small className="block text-ink-3">区间内成交</small>}</span>
      <span className={row.warning ? 'text-warn' : 'text-ink-2'}>{row.status}</span>
      <Field label="成交额"><span className="num">{amount(row.summary?.value ?? null)}</span></Field><Field label="滑点损耗 BP"><Slip summary={row.summary} /></Field><Field label="滑点成本"><Cost summary={row.summary} /></Field><Field label="耗时"><span className="num">{durationText(row.durationSec)}</span></Field>
    </button>
    <div id={panel} inert={!expansion.open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expansion.open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden"><ExecutionExpansion {...props} /></div></div>
  </div>
}

export function SymbolGroup({ row, accountId, scope, expansion, update, detailLink, currency, units, liveWindow }: Omit<RowProps, 'row'> & { row: JournalSymbol }) {
  const panel = `symbol-${encodeURIComponent(row.symbol)}`
  const fills = usePolling(useCallback((signal: AbortSignal) => getActivitySymbols(accountId, { ...liveWindow!, symbol: row.symbol }, signal), [accountId, liveWindow, row.symbol]), {
    queryKey: `journal-symbol-trades:${accountId}:${row.symbol}:${liveWindow?.since}:${liveWindow?.until}`,
    enabled: expansion.open && !scope && !!liveWindow, intervalMs: 0,
  })
  const trades = scope ? row.trades : fills.data?.data[0]?.trades ?? []
  return <div className="border-b border-line" data-journal-symbol={row.symbol}>
    <button type="button" aria-label={`${row.symbol}成交`} aria-expanded={expansion.open} aria-controls={panel} onClick={() => update({ ...expansion, open: !expansion.open })} className={`${SYMBOL_COLS} w-full items-baseline px-3 py-4 text-left text-[13px] hover:bg-bg-subtle ${expansion.open ? 'bg-bg-subtle' : ''}`}>
      <span className="col-span-2 break-all font-medium xl:col-span-1">{row.symbol}</span><Field label="成交额"><span className="num">{amount(row.summary.value)}</span></Field><Field label="滑点损耗 BP"><Slip summary={row.summary} /></Field><Field label="滑点成本"><Cost summary={row.summary} /></Field><Field label="成交笔数">{row.summary.count}</Field><Field label="最近成交"><span className="num text-xs text-ink-3">{row.lastTime ? shanghaiLabel(new Date(row.lastTime).toISOString()) : '—'}</span></Field><ChevronDown size={15} className={`text-ink-3 transition-transform motion-reduce:transition-none ${expansion.open ? 'rotate-180' : ''}`} />
    </button>
    <div id={panel} inert={!expansion.open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expansion.open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden"><div className="border-t border-line bg-bg-subtle/50 px-3 xl:px-6">
      <ErrorNotice intent="read" title="成交读取失败" hasData={fills.data != null} error={fills.error} onRetry={fills.refresh} />
      <TradeList accountId={accountId} scope={scope ? { ...scope, symbol: row.symbol } : undefined} trades={trades} expansion={expansion} update={update} currency={currency} units={units} detailLink={detailLink} scoped={!!scope && (scope.start != null || !!scope.day)} loading={!scope && fills.loading} />
    </div></div></div>
  </div>
}
