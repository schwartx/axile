import { useCallback, useState } from 'react'
import { useParams } from 'react-router'
import { RefreshCw, Search } from 'lucide-react'
import { Select } from '@/components/ui/Select'
import { AccountPageTitle } from '@/features/account/pageHead'
import { getSignalRecords, type SignalRecordStatus } from '@/features/signal/signalApi'
import { SignalRecordList } from '@/features/signal/SignalRecordList'
import { usePolling } from '@/lib/hooks/usePolling'
import { useDomainStore } from '@/stores/domain'

const RANGES = [
  { value: null, label: '全部时间' },
  { value: 7, label: '近 7 天' },
  { value: 30, label: '近 30 天' },
  { value: 90, label: '近 90 天' },
]
const STATUSES: { value: SignalRecordStatus | null; label: string }[] = [
  { value: null, label: '全部状态' },
  { value: 'SUCCEEDED', label: '执行完成' },
  { value: 'FAILED', label: '执行失败' },
]
const PAGE_BUTTON = 'min-h-9 rounded-md px-3 text-sm text-accent enabled:cursor-pointer enabled:hover:bg-fill disabled:text-ink-3 disabled:opacity-50'

export function SignalRecordsPage() {
  const { id } = useParams()
  return <SignalJournal key={id} accountId={Number(id)} />
}

function SignalJournal({ accountId }: { accountId: number }) {
  const account = useDomainStore(s => s.accounts?.find(item => item.account_id === accountId))
  const [days, setDays] = useState<number | null>(null)
  const [status, setStatus] = useState<SignalRecordStatus | null>(null)
  const [keyword, setKeyword] = useState('')
  const [cursors, setCursors] = useState<(number | null)[]>([null])
  const before = cursors.at(-1)
  const symbol = keyword.trim()
  const poll = usePolling(useCallback((signal: AbortSignal) =>
    getSignalRecords(accountId, { limit: 30, before, days, status, symbol }, signal),
  [accountId, before, days, status, symbol]), {
    queryKey: `signal-records:${accountId}:${before}:${days}:${status}:${symbol}`,
    intervalMs: before == null && !symbol ? 15000 : 0,
  })
  const next = poll.data?.next_cursor
  const resetPage = () => setCursors([null])

  return <section>
    <header className="mb-4 flex min-h-10 flex-wrap items-center gap-3">
      <AccountPageTitle accountId={accountId} page="信号记录" name={account?.name} channel={account?.trade_channel} market={account?.market} />
      <button type="button" aria-label="刷新信号记录" disabled={poll.loading || poll.refreshing}
        onClick={poll.refresh} className="ml-auto flex size-9 shrink-0 items-center justify-center rounded-md text-ink-3 enabled:cursor-pointer enabled:hover:bg-fill disabled:opacity-40">
        <RefreshCw size={16} aria-hidden className={poll.refreshing ? 'animate-spin motion-reduce:animate-none' : ''} />
      </button>
    </header>
    <div className="mb-4 flex flex-wrap items-center gap-3">
      <Select ariaLabel="时间范围" value={days} options={RANGES} onChange={value => { setDays(value); resetPage() }} />
      <label className="relative w-64 max-w-full">
        <Search aria-hidden size={15} className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-ink-3" />
        <input type="search" aria-label="搜索品种" placeholder="搜索品种" maxLength={80} value={keyword}
          onChange={event => { setKeyword(event.target.value); resetPage() }}
          className="w-full rounded-lg border border-line bg-surface py-1.5 pr-3 pl-9 text-sm text-ink-1 outline-none focus:border-accent" />
      </label>
      <Select ariaLabel="执行状态" value={status} options={STATUSES} onChange={value => { setStatus(value); resetPage() }} />
    </div>
    <SignalRecordList poll={poll} filtered={days != null || status != null || !!symbol} />
    {(cursors.length > 1 || next != null) && <nav aria-label="信号记录分页" className="flex items-center justify-end gap-3 border-t border-line py-3">
      <button type="button" className={PAGE_BUTTON} disabled={cursors.length === 1}
        onClick={() => setCursors(previous => previous.slice(0, -1))}>上一页</button>
      <span className="num text-xs text-ink-3">第 {cursors.length} 页</span>
      <button type="button" className={PAGE_BUTTON} disabled={next == null}
        onClick={() => { if (next != null) setCursors(previous => [...previous, next]) }}>下一页</button>
    </nav>}
  </section>
}
