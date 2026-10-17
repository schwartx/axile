import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { OverflowText } from '@/components/ui/OverflowText'
import { RecordTable, type RecordColumn } from '@/features/account/RecordTable'
import { operationEvents, type ActivityEvent } from '@/features/account/accountActivityModel'
import type { AccountActivity } from '@/lib/api/orders'
import { useToastStore } from '@/stores/ui'

function EventRow({ event, formatTime }: { event: ActivityEvent; formatTime: (time: string | null) => string }) {
  const [expanded, setExpanded] = useState(false)
  const panelId = useId()
  const toast = useToastStore(state => state.toast)
  const copy = async () => {
    if (!event.orderId) return
    try { await navigator.clipboard.writeText(event.orderId); toast('订单编号已复制') }
    catch { toast('复制失败，请选中编号手动复制') }
  }
  return <tbody className="border-b border-line" data-activity-record>
    <tr data-activity-row className="h-16 text-[13px] [&_td]:whitespace-nowrap [&_td]:px-3 [&_td]:py-3 [&_td:first-child]:pl-0 [&_td:last-child]:pr-0">
      <td className="num text-xs text-ink-3"><time dateTime={event.time ?? undefined}>{formatTime(event.time)}</time></td>
      <td className="font-medium"><OverflowText text={event.symbol || '账户通知'} /></td>
      <td><OverflowText text={event.operation || '未知操作'} /></td>
      <td className={event.warning ? 'text-warn' : 'text-ink-2'}><OverflowText text={event.label} /></td>
      <td className="text-ink-2"><OverflowText text={event.message || '—'} /></td>
      <td className="sticky right-0 bg-surface"><button type="button" aria-label={`${expanded ? '收起' : '展开'}记录 ${event.key}`} aria-expanded={expanded} aria-controls={panelId}
        onClick={() => setExpanded(value => !value)} className="flex min-h-8 w-full cursor-pointer items-center justify-end text-ink-3 hover:text-ink-1">
        <ChevronDown size={15} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`} />
      </button></td>
    </tr>
    <tr><td colSpan={6} className="p-0">
      <div id={panelId} aria-hidden={!expanded} inert={!expanded} className={`grid transition-[grid-template-rows,visibility] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr] visible' : 'grid-rows-[0fr] invisible'}`}>
        <div className="sticky left-0 min-h-0 min-w-0 max-w-[calc(92vw-44px)] overflow-hidden">
          <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-3 gap-y-2 pb-4 text-xs text-ink-2">
            <dt className="text-ink-3">完整原因</dt><dd className="min-w-0 whitespace-pre-wrap break-words">{event.message || '—'}</dd>
            {event.orderId && <><dt className="text-ink-3">订单编号</dt><dd className="flex min-w-0 items-start gap-3"><span className="min-w-0 select-text break-all">{event.orderId}</span><button type="button" aria-label="复制订单编号" onClick={() => void copy()} className="shrink-0 cursor-pointer text-accent">复制</button></dd></>}
          </dl>
        </div>
      </div>
    </td></tr>
  </tbody>
}

/** 独立的操作异常记录，不改变关联委托的状态。 */
export function OperationFailuresTable({ data, formatTime, loading, emptyText }: { data: AccountActivity | null; formatTime: (time: string | null) => string; loading?: boolean; emptyText?: string }) {
  const events = data ? operationEvents(data) : []
  const columns: RecordColumn[] = [
    { key: 'time', label: '发生时间', sample: '2026-12-28 23:59:59' },
    { key: 'symbol', label: '品种', sample: 'SHFE.rb2610', values: events.map(event => event.symbol || '账户通知'), maxWidth: 160 },
    { key: 'operation', label: '操作', sample: '撤单', values: events.map(event => event.operation ?? '') },
    { key: 'result', label: '结果', sample: '撤单失败 / 待确认', values: events.map(event => event.label) },
    { key: 'message', label: '失败原因', grow: 1, sample: '操作结果待确认，请核对渠道记录', values: events.map(event => event.message), maxWidth: 320 },
    { key: 'details', label: '详情' },
  ]
  return <RecordTable columns={columns} kind="event" loading={loading} empty={!events.length} emptyText={emptyText ?? '暂无操作异常'}>
    {events.map(event => <EventRow key={event.key} event={event} formatTime={formatTime} />)}
  </RecordTable>
}
