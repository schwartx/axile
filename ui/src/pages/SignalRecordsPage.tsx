import { useCallback, useState } from 'react'
import { useParams } from 'react-router'
import { JournalFilters, JournalPageHeader, JournalRefresh, JournalSymbolSearch } from '@/components/ui/JournalRecord'
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
  const queryKey = `signal-records:${accountId}:${before}:${days}:${status}:${symbol}`
  const poll = usePolling(useCallback((signal: AbortSignal) =>
    getSignalRecords(accountId, { limit: 30, before, days, status, symbol }, signal),
  [accountId, before, days, status, symbol]), {
    queryKey,
    intervalMs: before == null && !symbol ? 15000 : 0,
  })
  const next = poll.data?.next_cursor
  const resetPage = () => setCursors([null])

  return <section className="min-w-0 [&_button]:min-h-9 [&_button]:min-w-9">
    <JournalPageHeader
      title={<AccountPageTitle accountId={accountId} page="信号记录" name={account?.name} channel={account?.trade_channel} market={account?.market} />}
      actions={<JournalRefresh label="刷新信号记录" disabled={poll.loading || poll.refreshing}
        refreshing={poll.refreshing} onRefresh={() => void poll.refresh()} />} />
    <JournalFilters>
      <Select ariaLabel="时间范围" value={days} options={RANGES} onChange={value => { setDays(value); resetPage() }} />
      <JournalSymbolSearch value={keyword} maxLength={80} onChange={value => { setKeyword(value); resetPage() }} />
      <Select ariaLabel="执行状态" value={status} options={STATUSES} onChange={value => { setStatus(value); resetPage() }} />
    </JournalFilters>
    {!!poll.data?.items.length && <div className="flex min-h-14 items-center py-3 text-xs text-ink-3">
      本页 {poll.data.items.length} 条记录
    </div>}
    <SignalRecordList key={queryKey} poll={poll} filtered={days != null || status != null || !!symbol} />
    {(cursors.length > 1 || next != null) && <nav aria-label="信号记录分页" className="flex items-center justify-end gap-3 py-3">
      <button type="button" className={PAGE_BUTTON} disabled={cursors.length === 1}
        onClick={() => setCursors(previous => previous.slice(0, -1))}>上一页</button>
      <span className="num text-xs text-ink-3">第 {cursors.length} 页</span>
      <button type="button" className={PAGE_BUTTON} disabled={next == null}
        onClick={() => { if (next != null) setCursors(previous => [...previous, next]) }}>下一页</button>
    </nav>}
  </section>
}
