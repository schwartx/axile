import { useId, useRef, useState, type ReactNode } from 'react'
import { ChevronDown, Maximize } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { EdgeScrollHint } from '@/components/ui/EdgeScrollHint'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Skeleton } from '@/components/ui/Skeleton'
import { JournalEvidenceHeader, JournalEvidenceTable, JournalField as Field, JournalHeader, JournalRecordRow } from '@/components/ui/JournalRecord'
import { Segmented } from '@/components/ui/Segmented'
import { shanghaiLabel } from '@/features/history/costs'
import { useScrollEdges } from '@/hooks/useScrollEdges'
import type { PollingState } from '@/lib/hooks/usePolling'
import { formatObservedAt } from '@/lib/scheduleTime'
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
    {records.length > 0 && (compact ? <div className="divide-y divide-line">
      {records.map(record => <RecentSignalRow key={record.id} record={record} />)}
    </div> : <SignalJournalRows records={records} />)}
  </div>
}

function SignalJournalRows({ records }: { records: SignalRecord[] }) {
  const [expandedId, setExpandedId] = useState<string | null>(null)
  return <div aria-label="信号记录列表">
    <JournalHeader>
      <span>生成时间</span><span>信号摘要</span><span>状态</span>
      <span className="text-right">权益 ¥</span><span className="text-right">可用资金 ¥</span>
      <span className="text-right">手续费 ¥</span><span className="text-right">通知处理</span>
    </JournalHeader>
    {records.map(record => <SignalJournalRow key={record.id} record={record} open={expandedId === record.id}
      onToggle={() => setExpandedId(expandedId === record.id ? null : record.id)} />)}
  </div>
}

function SignalJournalRow({ record, open, onToggle }: { record: SignalRecord; open: boolean; onToggle: () => void }) {
  const panelId = useId()
  const failed = record.status === 'FAILED'
  const ledger = record.follow_ledger
  const notificationFailed = record.notification_status === 'failed'
  const expandable = !failed || !!record.scheduled_at || !!record.notification_error
  const detail = expandable && <div className="space-y-2 text-xs">
    <SignalRecordDiagnostics record={record} showError={!failed} />
    {ledger && ledger.status !== 'settled' && <p className="text-warn">{signalLedgerStatus(ledger)}</p>}
    {!failed && <SignalTargets record={record} detailed />}
  </div>
  return <JournalRecordRow panelId={panelId} open={open} onToggle={expandable ? onToggle : undefined} details={detail}
    time={<time dateTime={record.created_at}>{shanghaiLabel(record.created_at)}</time>}>
    <span className="col-span-2 min-w-0 break-words text-ink-2 xl:col-span-1">
      {signalSummary(record)}<SignalOrderCount record={record} />
      {failed && record.error && <span className="mt-1 block text-xs text-warn">{record.error}</span>}
    </span>
    <span className={failed || (ledger && ledger.status !== 'settled') ? 'text-warn' : 'text-ink-2'}>{signalResultLabel(record)}</span>
    <Field label="权益 ¥"><span className="num">{ledger ? quantity(ledger.equity) : '—'}</span></Field>
    <Field label="可用资金 ¥"><span className="num">{ledger ? quantity(ledger.cash) : '—'}</span></Field>
    <Field label="手续费 ¥"><span className="num">{ledger ? quantity(ledger.fee) : '—'}</span></Field>
    <Field label="通知处理"><span className={notificationFailed ? 'text-warn' : 'text-ink-2'}>{NOTIFICATION_LABELS[record.notification_status]}</span></Field>
  </JournalRecordRow>
}

function signalSummary(record: SignalRecord) {
  const source = record.trigger_source === 'manual' ? '手动生成' : '定时生成'
  if (record.status === 'FAILED') return source
  const count = Object.values(record.targets).filter(quantity => quantity !== 0).length
  return `${source} · ${count} 个目标品种`
}

function SignalOrderCount({ record }: { record: SignalRecord }) {
  const { adjustment } = record
  if (record.status === 'FAILED' || adjustment?.kind !== 'order') return null
  if (adjustment.status !== 'compared') return ' · 无下单指示'
  const count = Object.values(adjustment.changes).filter(change => change.delta !== 0).length
  return count ? ` · ${count} 单` : ' · 无需调仓'
}

export function signalResultLabel(record: SignalRecord) {
  if (record.status === 'FAILED') return '生成失败'
  if (record.followed_target) return '跟随已结转'
  if (record.follow_ledger) return '跟随未结转'
  return '信号已生成'
}

function RecentSignalRow({ record }: { record: SignalRecord }) {
  const failed = record.status === 'FAILED'
  const date = shanghaiLabel(record.created_at)
  const notification = NOTIFICATION_LABELS[record.notification_status]
  const columns = `${COLUMNS} px-3 py-3 pl-8 text-sm`
  const cells = <>
    <time className="num text-ink-3" dateTime={record.created_at}>{date.slice(5, 16)}</time>
    <span className="row-start-2 text-ink-2 sm:row-start-auto">{signalSummary(record)}</span>
    <span className={failed ? 'text-warn' : 'text-ink-1'}>{signalResultLabel(record)}</span>
    <span className={`text-right ${record.notification_status === 'failed' ? 'text-warn' : 'text-ink-3'}`}>
      <span className="sm:sr-only">通知</span>{notification}
    </span>
  </>
  if (!record.scheduled_at && !record.error && !record.notification_error) return <div className={columns}>{cells}</div>
  return <details className="group open:bg-surface">
    <summary className={`${columns} relative cursor-pointer list-none hover:bg-bg-subtle focus-visible:outline-offset-[-2px] group-open:bg-bg-subtle [&::-webkit-details-marker]:hidden`}>
      <ChevronDown aria-hidden size={14} className="absolute top-3.5 left-3 -rotate-90 text-ink-3 group-open:rotate-180" />
      {cells}
    </summary>
    <div className="px-3 pt-2 pb-3 pl-8 text-sm">
      <SignalRecordDiagnostics record={record} />
    </div>
  </details>
}

function SignalRecordDiagnostics({ record, showError = true }: { record: SignalRecord; showError?: boolean }) {
  return <>
    {record.scheduled_at && <p className="mb-2 text-ink-3">
      计划时间 <time className="num" dateTime={record.scheduled_at}>{shanghaiLabel(record.scheduled_at)}</time>
    </p>}
    {showError && record.error && <p className="mb-2 break-words text-warn">{record.error}</p>}
    {record.notification_error && <p className="mb-2 break-words text-warn">通知：{record.notification_error}</p>}
  </>
}

function signalLedgerStatus(ledger: NonNullable<SignalRecord['follow_ledger']>) {
  if (ledger.status === 'settled') return '已结转为信号持仓'
  return ledger.status === 'insufficient_cash' ? '资金不足，本次未跟随' : '目标含空头，本次未跟随'
}

function quantity(value: number, signed = false) {
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 12, signDisplay: signed ? 'exceptZero' : 'auto' })
}

function SignalOrderQuantity({ delta, unit, badge = true }: { delta: number; unit: string; badge?: boolean }) {
  if (!delta) return '—'
  const side = delta > 0 ? '买入' : '卖出'
  const amount = `${quantity(Math.abs(delta))} ${unit}`
  if (!badge) return `${side} ${amount}`
  return <span className="inline-flex items-baseline gap-2">
    <span className={`shrink-0 rounded-chip px-2 py-0.5 text-xs ${delta > 0 ? 'bg-up/15 text-up' : 'bg-down/15 text-down'}`}>
      {side}
    </span>
    <span className="num">{amount}</span>
  </span>
}

function orderInstructionNote(adjustment: NonNullable<SignalRecord['adjustment']>) {
  if (adjustment.status === 'unknown') return '持仓未知，无法生成下单指示'
  if (adjustment.status === 'unsupported') return '含空头持仓，无法生成 A 股下单指示'
  if (adjustment.status === 'compared' && !Object.keys(adjustment.changes).length) return '无需下单'
  return adjustment.basis === 'signal_holdings' ? '按结转前信号持仓' : '按生成时的实际持仓'
}

type SignalOrderRecord = Pick<SignalRecord, 'adjustment' | 'created_at' | 'quantity_unit' | 'settlement_prices' | 'follow_ledger'>

function signalOrderAmount(delta: number | undefined, settlementPrice: number | undefined) {
  return delta && settlementPrice != null ? Math.abs(delta) * settlementPrice : null
}

function signalAmountText(amount: number | null | undefined) {
  return amount == null ? '—' : amount.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function SignalOrderEvidence({ record, orders }: { record: SignalOrderRecord; orders: [string, { delta: number }][] }) {
  const { quantity_unit: unit, settlement_prices: prices, follow_ledger: ledger } = record
  const estimate = ledger?.status === 'insufficient_cash'
  const rows = orders.map(([symbol, { delta }]) => {
    const price = prices?.[symbol]
    return { symbol, delta, price, amount: signalOrderAmount(delta, price) }
  })
  const totals: Record<'buy' | 'sell', number | null> = { buy: 0, sell: 0 }
  for (const row of rows) {
    const side = row.delta > 0 ? 'buy' : 'sell'
    const total = totals[side]
    totals[side] = total == null || row.amount == null ? null : total + row.amount
  }

  return <div className="space-y-3">
    <div className="overflow-x-auto">
      <JournalEvidenceTable label="最近一次信号下单明细">
        <thead className="border-b border-line text-ink-3">
          <tr>
            <th scope="col">品种</th><th scope="col">买卖与股数</th>
            <th scope="col" className="text-right">结转价（¥）</th>
            <th scope="col" className="text-right">{estimate ? '测算金额' : '金额'}（¥）</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map(({ symbol, delta, price, amount }) => <tr key={symbol}>
            <th scope="row" className="font-medium">{symbol}</th>
            <td><SignalOrderQuantity delta={delta} unit={unit} /></td>
            <td className="num text-right">{price == null ? '—' : quantity(price)}</td>
            <td className="num text-right">{signalAmountText(amount)}</td>
          </tr>)}
        </tbody>
      </JournalEvidenceTable>
    </div>
    <div className="space-y-2 border-t border-line pt-3 pb-2 text-xs">
      {ledger && ledger.status !== 'settled' && <p className="text-warn">{signalLedgerStatus(ledger)}</p>}
      <dl className="flex flex-wrap gap-x-4 gap-y-2">
        <div className="flex items-baseline gap-1.5"><dt className="text-ink-2">{estimate && '测算'}买入金额（¥）</dt><dd className="num">{signalAmountText(totals.buy)}</dd></div>
        <div className="flex items-baseline gap-1.5"><dt className="text-ink-2">{estimate && '测算'}卖出金额（¥）</dt><dd className="num">{signalAmountText(totals.sell)}</dd></div>
        <div className="flex items-baseline gap-1.5"><dt className="text-ink-2">手续费（¥）</dt><dd className="num">{signalAmountText(ledger?.fee)}</dd></div>
      </dl>
    </div>
  </div>
}

export function SignalOrderInstructions({ record }: { record: SignalOrderRecord }) {
  const [expandedAt, setExpandedAt] = useState<string | null>(null)
  const { adjustment, created_at: createdAt, quantity_unit: unit } = record
  if (adjustment?.kind !== 'order') return null
  const changes = adjustment.status === 'compared'
    ? Object.entries(adjustment.changes).filter(([, { delta }]) => delta !== 0)
    : []
  const date = shanghaiLabel(createdAt)
  const timeLabel = formatObservedAt(createdAt, undefined, 'second')
  const orders = <ul className="space-y-1 text-ink-1">
    {changes.map(([symbol, { delta }]) => <li key={symbol} className="flex items-baseline justify-between gap-3 py-1">
      <SignalOrderQuantity delta={delta} unit={unit} />
      <span className="text-right">{symbol}</span>
    </li>)}
  </ul>

  return <section className="group/signal mt-2.5 flex min-h-0 flex-1 flex-col border-t border-line pt-3 text-sm">
    <div className="flex shrink-0 items-center justify-between gap-x-3">
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
        <h4 className="shrink-0 text-base font-semibold text-ink-1">最近一次信号</h4>
        <p className="text-xs text-ink-3">
          <time className="num" dateTime={createdAt} title={date}>{timeLabel}</time>
          {(!changes.length || adjustment.basis !== 'signal_holdings') && ` · ${orderInstructionNote(adjustment)}`}
        </p>
      </div>
      {changes.length > 0 && <button type="button" aria-haspopup="dialog" aria-label="展开买卖列表" title="展开买卖列表" onClick={() => setExpandedAt(createdAt)}
        className="pointer-events-none inline-flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-md text-ink-3 opacity-0 transition-opacity hover:bg-fill hover:text-ink-1 group-hover/signal:pointer-events-auto group-hover/signal:opacity-100 group-focus-within/signal:pointer-events-auto group-focus-within/signal:opacity-100 motion-reduce:transition-none [@media(hover:none)]:pointer-events-auto [@media(hover:none)]:opacity-100">
        <Maximize size={14} aria-hidden />
      </button>}
    </div>
    {changes.length > 0 && <SignalOrderList>{orders}</SignalOrderList>}
    {expandedAt === createdAt && changes.length > 0 && <CenteredModal open title="最近一次信号" subtitle={timeLabel}
      widthClassName="w-[620px]" onClose={() => setExpandedAt(null)}>
      <SignalOrderEvidence record={record} orders={changes} />
    </CenteredModal>}
  </section>
}

function SignalOrderList({ children }: { children: ReactNode }) {
  const list = useRef<HTMLDivElement>(null)
  const edges = useScrollEdges(list)
  return <div className="relative mt-2 min-h-0 flex-1">
    <div ref={list} role="region" aria-label="最近一次信号的买卖列表"
      tabIndex={edges.overflowing ? 0 : undefined} className="quiet-scrollbar h-full overflow-y-auto overscroll-contain">
      {children}
    </div>
    <EdgeScrollHint edge="up" visible={edges.up} target={list} label="买卖列表" />
    <EdgeScrollHint edge="down" visible={edges.down} target={list} label="买卖列表" />
  </div>
}

export function SignalTargets({ record, detailed = false }: { record: SignalRecord; detailed?: boolean }) {
  const [view, setView] = useState<'signal' | 'all'>('signal')
  const {
    node_change: node, adjustment, targets, quantity_unit: unit, follow_ledger: ledger,
    sizing_prices: sizingPrices, settlement_prices: settlementPrices,
  } = record
  const changes = node?.status === 'compared' ? node.changes : null
  const adjustments = adjustment?.status === 'compared' ? adjustment.changes : null
  const symbols = [...new Set([...Object.keys(targets), ...Object.keys(changes ?? {}), ...Object.keys(adjustments ?? {})])]
  const relevantSymbols = detailed ? symbols.filter(symbol =>
    (targets[symbol] ?? 0) !== 0 || (changes?.[symbol]?.delta ?? 0) !== 0 || (adjustments?.[symbol]?.delta ?? 0) !== 0
  ) : symbols
  const visibleSymbols = view === 'all' ? symbols : relevantSymbols
  const hasHiddenSymbols = symbols.length > relevantSymbols.length
  const nodeNote = node?.status === 'first' ? '首次生成'
    : node?.status === 'missing' ? '上一节点无结果'
      : changes && !Object.keys(changes).length ? '较上一节点无变化' : null
  const orderInstructions = adjustment?.kind === 'order'
  const showSettlement = detailed && orderInstructions
  const adjustmentNote = orderInstructions ? orderInstructionNote(adjustment)
    : adjustment?.status === 'unknown' ? '实仓未知，待调整不可用'
      : adjustments && !Object.keys(adjustments).length ? '实仓已对齐目标' : null
  return <div className="space-y-1">
    <div className={detailed ? 'min-w-0 space-y-2' : 'min-w-0 space-y-1'}>
      {detailed && <JournalEvidenceHeader label={adjustmentNote ?? '信号目标持仓'}
        actions={hasHiddenSymbols && <Segmented size="sm" ariaLabel="信号品种范围" value={view}
          onChange={setView} options={[{ value: 'signal', label: '本次信号' }, { value: 'all', label: '全部品种' }]} />} />}
      <div className="overflow-x-auto">
        {visibleSymbols.length ? <JournalEvidenceTable label="信号目标持仓" className={detailed ? undefined : 'w-full max-w-lg text-left'}>
          <thead className="border-b border-line text-xs text-ink-3">
            <tr>
              <th scope="col" className="py-1.5 pr-3 font-medium">品种</th>
              <th scope="col" className="whitespace-nowrap py-1.5 text-right font-medium">目标（{unit}）</th>
              {node && <th scope="col" className="whitespace-nowrap py-1.5 pl-3 text-right font-medium">较上一节点</th>}
              {adjustment && <th scope="col" className="whitespace-nowrap py-1.5 pl-3 text-right font-medium">{orderInstructions ? '下单指示' : '待调整'}</th>}
              {detailed && <th scope="col" className="text-right font-medium">换算价</th>}
              {showSettlement && <>
                <th scope="col" className="text-right font-medium">结转价（¥）</th>
                <th scope="col" className="text-right font-medium">{ledger?.status === 'insufficient_cash' ? '测算金额' : '金额'}（¥）</th>
              </>}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {visibleSymbols.map(symbol => {
              const change = changes?.[symbol]
              const pending = adjustments?.[symbol]
              const sizingPrice = sizingPrices?.[symbol]
              const settlementPrice = settlementPrices?.[symbol]
              return <tr key={symbol}>
                <th scope="row" className="py-1.5 pr-3 text-left font-medium [overflow-wrap:anywhere]">{symbol}</th>
                <td className="num py-1.5 text-right">{quantity(targets[symbol] ?? 0)}</td>
                {node && <td className="num whitespace-nowrap py-1.5 pl-3 text-right">
                  {change && detailed && <span className="mr-2 text-xs text-ink-3">
                    {quantity(change.before)} → {quantity(change.after)}
                  </span>}
                  {changes ? quantity(change?.delta ?? 0, true) : '—'}
                </td>}
                {adjustment && <td className="num whitespace-nowrap py-1.5 pl-3 text-right">
                  {pending && <span className="sr-only">生成时持仓 {quantity(pending.before)}，</span>}
                  {orderInstructions ? <SignalOrderQuantity delta={pending?.delta ?? 0} unit={unit} badge={detailed} />
                    : adjustments ? quantity(pending?.delta ?? 0, true) : '—'}
                </td>}
                {detailed && <td className="num text-right">{sizingPrice == null ? '—' : quantity(sizingPrice)}</td>}
                {showSettlement && <>
                  <td className="num text-right">{settlementPrice == null ? '—' : quantity(settlementPrice)}</td>
                  <td className="num text-right">{signalAmountText(signalOrderAmount(pending?.delta, settlementPrice))}</td>
                </>}
              </tr>
            })}
          </tbody>
        </JournalEvidenceTable> : <p className="text-ink-3">目标为空仓</p>}
      </div>
      {nodeNote && <p className="text-xs text-ink-3">{nodeNote}</p>}
      {!detailed && adjustmentNote && <p className="text-xs text-ink-3">{adjustmentNote}</p>}
    </div>
    {!detailed && (ledger ? <div className="space-y-1 pt-2 text-xs text-ink-3">
      <p>{signalLedgerStatus(ledger)}</p>
      <p className="num">权益 ¥{quantity(ledger.equity)} · 可用资金 ¥{quantity(ledger.cash)} · 手续费 ¥{quantity(ledger.fee)}</p>
    </div> : record.followed_target && <p className="text-xs text-ink-3">已结转为信号持仓</p>)}
  </div>
}
