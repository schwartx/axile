import { ChevronDown } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Skeleton } from '@/components/ui/Skeleton'
import { shanghaiLabel } from '@/features/history/costs'
import type { PollingState } from '@/lib/hooks/usePolling'
import type { SignalRecord, SignalRecords } from './signalApi'

const COLUMNS = 'grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 sm:grid-cols-[11rem_minmax(0,1fr)_7rem_6rem]'
const NOTIFICATION_LABELS: Record<SignalRecord['notification_status'], string> = {
  pending: '待处理',
  calling: '处理中',
  succeeded: '已处理',
  failed: '失败',
  unknown: '结果未知',
}

export function SignalRecordList({ poll, compact = false, filtered = false }: {
  poll: PollingState<SignalRecords>
  compact?: boolean
  filtered?: boolean
}) {
  const records = poll.data?.items ?? []
  return <div aria-busy={poll.loading}>
    <ErrorNotice intent="read" title="信号记录加载失败" error={poll.error} hasData={poll.data != null}
      variant={poll.stale ? 'stale' : 'section'} updatedAt={poll.updatedAt} onRetry={poll.refresh} />
    {poll.loading && <div aria-label="正在加载信号记录" className="space-y-4 border-y border-line px-3 py-4">
      <Skeleton className="h-3 w-32" />
      <Skeleton className="h-3 w-52 max-w-full" />
    </div>}
    {poll.data && records.length === 0 && <p className="px-4 py-6 text-sm text-ink-3">
      {filtered ? '没有符合筛选条件的信号记录' : '暂无信号记录'}
    </p>}
    {records.length > 0 && <>
      {!compact && <div aria-hidden className={`${COLUMNS} hidden border-y border-line px-3 py-2 pl-8 text-xs text-ink-3 sm:grid`}>
        <span>执行时间</span><span>信号摘要</span><span>执行状态</span><span className="text-right">通知处理</span>
      </div>}
      <div className="divide-y divide-line">
        {records.map(record => <SignalRecordRow key={record.id} record={record} compact={compact} />)}
      </div>
    </>}
  </div>
}

function SignalRecordRow({ record, compact }: { record: SignalRecord; compact: boolean }) {
  const failed = record.status === 'FAILED'
  const source = record.trigger_source === 'manual' ? '手动执行' : '自动执行'
  const entries = Object.entries(record.targets)
  const count = entries.filter(([, quantity]) => quantity !== 0).length
  const date = shanghaiLabel(record.created_at)
  const notification = NOTIFICATION_LABELS[record.notification_status]
  return <details className="group open:bg-surface">
    <summary className={`${COLUMNS} relative cursor-pointer list-none px-3 py-3 pl-8 text-sm hover:bg-bg-subtle focus-visible:outline-offset-[-2px] [&::-webkit-details-marker]:hidden`}>
      <ChevronDown aria-hidden size={14} className="absolute top-3.5 left-3 -rotate-90 text-ink-3 group-open:rotate-180" />
      <time className="num text-ink-3" dateTime={record.created_at}>{compact ? date.slice(5, 16) : date}</time>
      <span className="row-start-2 text-ink-2 sm:row-start-auto">{source}{!failed && ` · ${count} 个目标品种`}</span>
      <span className={failed ? 'text-warn' : 'text-ink-1'}>{failed ? '执行失败' : '执行完成'}</span>
      <span className={`text-right ${record.notification_status === 'failed' ? 'text-warn' : 'text-ink-3'}`}>
        <span className="sm:sr-only">通知</span>{notification}
      </span>
    </summary>
    <div className="border-l-2 border-accent bg-bg px-4 py-3 text-sm">
      {record.scheduled_at && <p className="mb-2 text-ink-3">
        计划时间 <time className="num" dateTime={record.scheduled_at}>{shanghaiLabel(record.scheduled_at)}</time>
      </p>}
      {record.error && <p className="mb-2 break-words text-warn">{record.error}</p>}
      {record.notification_error && <p className="mb-2 break-words text-warn">通知：{record.notification_error}</p>}
      {!failed && (entries.length ? <table className="w-full max-w-lg text-left">
        <thead className="border-b border-line text-xs text-ink-3">
          <tr><th className="py-2 font-medium">品种</th><th className="py-2 text-right font-medium">目标数量（{record.quantity_unit}）</th></tr>
        </thead>
        <tbody className="divide-y divide-line">
          {entries.map(([symbol, quantity]) => <tr key={symbol}>
            <td className="py-1.5 [overflow-wrap:anywhere]">{symbol}</td>
            <td className="num py-1.5 text-right">{quantity.toLocaleString('zh-CN', { maximumFractionDigits: 12 })}</td>
          </tr>)}
        </tbody>
      </table> : <p className="text-ink-3">目标为空仓</p>)}
    </div>
  </details>
}
