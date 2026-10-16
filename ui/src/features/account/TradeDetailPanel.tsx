import type { ReactNode } from 'react'
import type { AccountTrade } from '@/lib/api/orders'
import { tradeDetails } from '@/features/account/tradeDetails'
import { useToastStore } from '@/stores/ui'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="grid min-w-0 grid-cols-[120px_minmax(0,1fr)] gap-x-3">
    <dt className="text-ink-3">{label}</dt><dd className="min-w-0 break-all">{children}</dd>
  </div>
}

function Identifier({ value, label }: { value: string; label: string }) {
  const toast = useToastStore(state => state.toast)
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); toast(`${label}已复制`) }
    catch { toast('复制失败，请选中编号手动复制') }
  }
  return <span className="inline-flex max-w-full items-start gap-3"><span className="min-w-0 select-text break-all">{value}</span>
    <button type="button" onClick={() => void copy()} className="shrink-0 cursor-pointer text-accent" aria-label={`复制${label}`}>复制</button>
  </span>
}

export function TradeDetailPanel({ trade, channel, formatTime }: {
  trade: AccountTrade; channel?: string; formatTime: (value: string | null) => string
}) {
  return <div className="@container pb-4 text-xs text-ink-2"><dl className="grid grid-cols-1 gap-x-6 gap-y-2 @xl:grid-cols-2 @4xl:grid-cols-3">
    <Field label="订单编号">{trade.order_id ? <Identifier value={trade.order_id} label="订单编号" /> : '未知'}</Field>
    <Field label="成交编号"><Identifier value={trade.trade_id} label="成交编号" /></Field>
    <Field label="记录时间">{formatTime(trade.time)}</Field>
    {tradeDetails(trade, channel).map(field => <Field key={field.key} label={field.label}>
      {field.copyable ? <Identifier value={field.text} label={field.label} /> : field.text}
    </Field>)}
  </dl></div>
}
