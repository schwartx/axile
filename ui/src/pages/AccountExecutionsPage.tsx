import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useParams, useSearchParams } from 'react-router'
import { RefreshCw, Search, ExternalLink } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Skeleton } from '@/components/ui/Skeleton'
import { Segmented } from '@/components/ui/Segmented'
import { Select } from '@/components/ui/Select'
import { AccountPageTitle } from '@/features/account/pageHead'
import { journalWindow, type JournalRange } from '@/features/account/executionJournal'
import { ExecutionGroup, SymbolGroup, EXEC_COLS, SYMBOL_COLS, type Expansion } from '@/features/account/JournalRows'
import { parseJournalScope, scopeLabel, SNAPSHOT_PARAMS, loadSnapshotJournal, loadLiveJournal, filterLiveExecutions, compareJournal, journalTotals, activityWindowQuery } from '@/features/account/journalSource'
import { amount, coverageText, shanghaiLabel, lossClass } from '@/features/history/costs'
import { usePolling } from '@/lib/hooks/usePolling'
import { useRemountFade } from '@/lib/viewTransition'
import { useDomainStore } from '@/stores/domain'
import { useChannelDescriptor } from '@/stores/channels'
import { ApiError } from '@/lib/api/client'

const VIEWS = [{ value: 'executions', label: '按执行' }, { value: 'symbols', label: '按品种' }]
const RANGES: { value: JournalRange; label: string }[] = [{ value: '7', label: '近 7 天' }, { value: '30', label: '近 30 天' }, { value: '90', label: '近 90 天' }, { value: 'custom', label: '自定义' }]
const STATUSES = ['全部状态', '已完成', '执行不到位', '执行失败', '已终止', '已跳过', '执行结果待确认', '历史执行记录'].map(label => ({ value: label, label }))
const SORTS = [{ value: 'time', label: '时间从近到远' }, { value: 'value', label: '成交额从高到低' }, { value: 'slippage', label: '滑点损耗优先' }, { value: 'cost', label: '滑点成本从高到低' }]
const INPUT = 'min-w-0 rounded-lg border border-line bg-surface px-3 py-1.5 text-[13px] text-ink-1 outline-none focus:border-accent'
interface Visit { expanded: Record<string, Expansion>; count: number; scroll: number }
const closedExpansion = (): Expansion => ({ open: false, count: 20, side: 'all' })
const visits = new Map<string, Visit>()
const today = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10)

export function AccountExecutionsPage() {
  const { id } = useParams()
  return <ExecutionJournal key={id} accountId={Number(id)} />
}
function ExecutionJournal({ accountId }: { accountId: number }) {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const item = useDomainStore(s => s.accounts?.find(a => a.account_id === accountId))
  const descriptor = useChannelDescriptor(item?.trade_channel)
  const currency = item?.currency ?? ''
  const view = params.get('view') === 'symbols' ? 'symbols' : 'executions'
  const range = (RANGES.find(r => r.value === params.get('range'))?.value ?? '30')
  const from = params.get('from') ?? today(), to = params.get('to') ?? today()
  const keyword = params.get('symbol') ?? ''
  const status = STATUSES.find(s => s.value === params.get('status'))?.value ?? '全部状态'
  const sort = SORTS.find(s => s.value === params.get('sort'))?.value ?? (view === 'executions' ? 'time' : 'value')
  const parsed = parseJournalScope(params)
  const scopeKey = JSON.stringify(parsed.scope)
  const scope = useMemo(() => JSON.parse(scopeKey) as typeof parsed.scope, [scopeKey])
  const window = journalWindow(range, from, to)
  const start = window?.start ?? 0, end = window?.end ?? 0
  const liveWindow = !scope && window ? activityWindowQuery(window) : undefined
  const poll = usePolling(useCallback((signal: AbortSignal) => scope
    ? loadSnapshotJournal(accountId, scope, view, keyword, signal)
    : loadLiveJournal(accountId, { start, end }, signal, view), [accountId, scope, view, keyword, start, end]), {
    queryKey: scope ? `journal:${accountId}:${scopeKey}:${view}:${keyword}` : `journal:${accountId}:${start}:${end}:${view}`,
    intervalMs: 0, enabled: !parsed.error && (!!scope || !!window),
  })
  const filtered = useMemo(() => {
    const rows = poll.data?.executions ?? []
    return (scope ? rows : filterLiveExecutions(rows, keyword)).filter(row => status === '全部状态' || row.status === status).toSorted((a, b) => compareJournal(a, b, sort))
  }, [poll.data, scope, keyword, status, sort])
  const symbols = useMemo(() => (poll.data?.symbols ?? []).filter(row => scope || row.symbol.toLowerCase().includes(keyword.trim().toLowerCase())).toSorted((a, b) => compareJournal(a, b, sort)), [poll.data, scope, keyword, sort])
  const summary = useMemo(() => journalTotals(view === 'executions' ? filtered : symbols), [view, filtered, symbols])
  const fade = useRemountFade(view)
  const visitKey = `${accountId}:${location.search}`
  const initial = useRef(visits.get(visitKey))
  const [expanded, setExpanded] = useState<Record<string, Expansion>>(initial.current?.expanded ?? (scope?.record_id ? { [`execution:${scope.record_id}`]: { ...closedExpansion(), open: true } } : {}))
  const [count, setCount] = useState(initial.current?.count ?? 50)
  const restoreScroll = useRef<number | null>(initial.current?.scroll ?? null)
  const located = useRef(initial.current != null)
  const snapshot = useRef<Visit>({ expanded, count, scroll: initial.current?.scroll ?? 0 })
  snapshot.current = { ...snapshot.current, expanded, count }
  useEffect(() => {
    const main = document.querySelector('main.app-workspace')
    const save = () => { snapshot.current.scroll = main?.scrollTop ?? 0 }
    main?.addEventListener('scroll', save, { passive: true })
    return () => { main?.removeEventListener('scroll', save); visits.set(visitKey, snapshot.current) }
  }, [visitKey])
  useLayoutEffect(() => {
    if (!poll.data) return
    const main = document.querySelector('main.app-workspace')
    if (restoreScroll.current != null) {
      const scroll = restoreScroll.current
      if (!main) return
      // Expanded evidence arrives after the list. Keep the target until layout can contain it.
      let frame = 0
      const finish = () => { restoreScroll.current = null; observer.disconnect() }
      const restore = () => {
        frame = requestAnimationFrame(() => {
          main.scrollTop = scroll
          snapshot.current.scroll = main.scrollTop
          if (Math.abs(main.scrollTop - scroll) < 2) finish()
        })
      }
      const observer = new ResizeObserver(restore)
      const content = main.querySelector('section')
      if (content) observer.observe(content)
      main.addEventListener('wheel', finish, { passive: true })
      main.addEventListener('pointerdown', finish)
      restore()
      return () => { cancelAnimationFrame(frame); observer.disconnect(); main.removeEventListener('wheel', finish); main.removeEventListener('pointerdown', finish) }
    }
    if (!located.current && scope?.record_id) {
      located.current = true
      document.querySelector(`[data-journal-record="${scope.record_id}"]`)?.scrollIntoView({ block: 'start' })
    }
  }, [poll.data, scope])
  const update = (key: string, value: string) => {
    visits.set(visitKey, { ...snapshot.current })
    const next = new URLSearchParams(params)
    next.set(key, value)
    if (key === 'range' || key === 'from' || key === 'to') SNAPSHOT_PARAMS.forEach(k => next.delete(k))
    setParams(next, { replace: true, preventScrollReset: true })
    setCount(50)
    if (key === 'symbol' || key === 'range' || key === 'from' || key === 'to') setExpanded(previous => Object.fromEntries(Object.entries(previous).map(([id, state]) => [id, { ...state, cursors: [undefined], count: 20 }])))
  }
  const updateExpansion = (key: string, next: Expansion) => setExpanded(previous => ({ ...previous, [key]: next }))
  const detailLink = (executionId: string | null) => executionId ? <Link to={`/accounts/${accountId}/executions/${executionId}`} state={{ journalReturn: `${location.pathname}${location.search}` }}
    onClick={() => visits.set(visitKey, { ...snapshot.current, scroll: document.querySelector('main.app-workspace')?.scrollTop ?? 0 })}
    className="inline-flex min-h-9 items-center gap-1 text-accent hover:underline">完整执行详情 <ExternalLink size={13} aria-hidden /></Link> : <span className="text-ink-3">无执行详情</span>
  const expired = poll.error instanceof ApiError && poll.error.status === 410
  const scopedQuery = scope ? { ...scope, symbol_search: keyword.trim() || undefined } : undefined
  return <section className="min-w-0 [&_button]:min-h-9 [&_button]:min-w-9">
    <div className="mb-5 flex items-center justify-between gap-3"><div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1"><AccountPageTitle accountId={accountId} page="执行记录" name={item?.name} channel={item?.trade_channel} market={item?.market} /></div>
      <button type="button" aria-label="刷新执行记录" title="刷新执行记录" disabled={poll.loading || poll.refreshing || !!parsed.error || expired} onClick={() => void poll.refresh()} className="flex size-9 shrink-0 items-center justify-center rounded-md text-ink-3 hover:bg-fill disabled:opacity-40"><RefreshCw size={16} className={poll.refreshing ? 'animate-spin motion-reduce:animate-none' : ''} /></button></div>
    {(scope || parsed.error) && <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-ink-3" data-testid="journal-source">
      <Link to={`/accounts/${accountId}/history`} className="inline-flex min-h-9 items-center text-accent">返回实盘绩效</Link>
      {scope && <><span>来自实盘绩效 · {scopeLabel(scope)}</span>{poll.data?.dataUntil && <span>快照数据截止 {shanghaiLabel(poll.data.dataUntil)}</span>}</>}
      <button className="min-h-9 text-accent" onClick={() => update('range', '30')}>查看最新记录</button>
    </div>}
    <div className="flex flex-wrap items-center gap-3 border-b border-line pb-4">
      <Segmented size="sm" value={view} options={VIEWS} onChange={value => update('view', value)} />
      <Select ariaLabel="时间范围" value={scope ? 'snapshot' : range} options={scope ? [{ value: 'snapshot', label: '绩效选定范围' }, ...RANGES] : RANGES} onChange={value => { if (value !== 'snapshot') update('range', value) }} />
      <label className="relative min-w-0 flex-1 basis-40 sm:max-w-64"><Search size={14} className="pointer-events-none absolute left-2.5 top-2.5 text-ink-3" aria-hidden /><input aria-label="搜索品种" placeholder="搜索品种" value={keyword} onChange={e => update('symbol', e.target.value)} className={`${INPUT} w-full pl-8`} /></label>
      {view === 'executions' && <Select ariaLabel="执行状态" value={status} options={STATUSES} onChange={value => update('status', value)} />}
      <Select ariaLabel="排序" value={sort} options={SORTS} onChange={value => update('sort', value)} />
    </div>
    <div inert={!!scope || range !== 'custom'} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${!scope && range === 'custom' ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden"><div className="flex flex-wrap items-center gap-3 py-3">
      <label className="flex min-w-0 items-center gap-2 text-xs text-ink-3">开始<input aria-label="开始日期" type="date" value={from} max={to} onChange={e => update('from', e.target.value)} className={INPUT} /></label>
      <label className="flex min-w-0 items-center gap-2 text-xs text-ink-3">结束<input aria-label="结束日期" type="date" value={to} min={from} onChange={e => update('to', e.target.value)} className={INPUT} /></label>
    </div></div></div>
    {parsed.error && <p role="alert" className="py-4 text-sm text-warn">{parsed.error.message}</p>}
    {!scope && !window && <p role="alert" className="py-4 text-sm text-warn">请选择有效的起止日期，结束日期不能早于开始日期。</p>}
    {expired ? <p role="alert" className="py-4 text-sm text-warn">绩效快照已失效，请返回实盘绩效刷新后重新选择区间。</p> : <ErrorNotice title="执行记录加载失败" error={poll.error} variant={poll.data ? 'stale' : 'section'} updatedAt={poll.updatedAt} onRetry={poll.refresh} />}
    {poll.loading && <div aria-busy="true" aria-label="正在加载执行记录">{Array.from({ length: 7 }, (_, i) => <div key={i} className="flex min-h-20 items-center gap-6 border-b border-line"><Skeleton className="h-3 w-28" /><Skeleton className="h-3 w-1/3" /></div>)}</div>}
    {poll.data && !expired && <div key={view} className={fade ? 'panel-fade-in' : ''}>
      <div data-testid="journal-summary" className="flex min-h-14 flex-wrap items-center gap-x-5 gap-y-2 py-3 text-xs text-ink-3">
        <span>{view === 'executions' ? `${filtered.length} 条记录` : `${symbols.length} 个品种`} · 成交 {summary.count} 笔</span>
        <span>{summary.amountComplete ? '成交额' : '已知成交额'} {amount(summary.value)} {currency}</span>
        <span className={lossClass(summary.lossBp)}>滑点损耗 {amount(summary.lossBp)} BP</span>
        <span className={lossClass(summary.cost)}>{summary.covered < summary.count ? '已知滑点成本' : '滑点成本'} {amount(summary.cost)} {currency}</span>
        <span>{coverageText(summary)}</span>{!!summary.estimated && <span>{summary.estimated} 笔使用执行时间</span>}
        <span title="按有效成交额加权；参考价优先到达中间价，其次到达最新价。">正值为损耗，负值为改善</span>
      </div>
      {view === 'executions' ? <>
        <div className={`${EXEC_COLS} hidden border-y border-line px-3 py-2 text-xs text-ink-3 xl:grid`}><span>执行时间</span><span>执行摘要</span><span>状态</span><span className="text-right">成交额 {currency}</span><span className="text-right">滑点损耗 BP</span><span className="text-right">滑点成本 {currency}</span><span className="text-right">耗时</span></div>
        {filtered.slice(0, count).map(row => <ExecutionGroup key={`${row.key}:${scopeKey}:${keyword}`} row={row} accountId={accountId} scope={scopedQuery} expansion={expanded[row.key] ?? closedExpansion()} update={next => updateExpansion(row.key, next)} detailLink={detailLink} currency={currency} units={descriptor?.units} liveWindow={liveWindow} />)}
        {filtered.length === 0 && <p className="py-16 text-center text-sm text-ink-3">所选范围内无匹配记录</p>}
      </> : <>
        <div className={`${SYMBOL_COLS} hidden border-y border-line px-3 py-2 text-xs text-ink-3 xl:grid`}><span>品种</span><span className="text-right">成交额 {currency}</span><span className="text-right">滑点损耗 BP</span><span className="text-right">滑点成本 {currency}</span><span className="text-right">成交笔数</span><span className="text-right">最近成交</span><span /></div>
        {symbols.slice(0, count).map(row => <SymbolGroup key={`${row.symbol}:${scopeKey}:${keyword}`} row={row} accountId={accountId} scope={scopedQuery} expansion={expanded[`symbol:${row.symbol}`] ?? closedExpansion()} update={next => updateExpansion(`symbol:${row.symbol}`, next)} detailLink={detailLink} currency={currency} units={descriptor?.units} liveWindow={liveWindow} />)}
        {symbols.length === 0 && <p className="py-16 text-center text-sm text-ink-3">所选范围内无匹配成交</p>}
      </>}
      {(view === 'executions' ? filtered.length : symbols.length) > count && <button className="min-h-9 py-3 text-xs text-accent" onClick={() => setCount(count + 50)}>加载更多记录</button>}
    </div>}
  </section>
}
