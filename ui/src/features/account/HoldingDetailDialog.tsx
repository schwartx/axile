import { useId, useState } from 'react'
import { ChevronDown, Info, RefreshCw } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Tooltip } from '@/components/ui/Tooltip'
import { CurrentOrdersContent } from '@/features/account/CurrentOrders'
import { holdingActions, holdingBook, holdingTarget, projectHolding, sameHoldingSymbol, type HoldingAction, type HoldingBook, type HoldingProjection, type HoldingTarget } from '@/features/account/holdingDetailModel'
import { executableTargetFallbackText, quantityText, sizingAvailabilityText, sizingFormulas, sizingNumber, sizingReasonText, weightText } from '@/features/account/sizingEvidenceModel'
import type { OrdersState } from '@/features/account/useCurrentOrders'
import { displayCurrencyUnit } from '@/lib/format'
import type { ChannelCapability, Position, TargetSizingRow, TargetWeightSnapshot } from '@/types/api'

interface HoldingDetailProps {
  symbol: string | null
  positions: Position[]
  observed: boolean
  observedAt: string | null
  target: TargetWeightSnapshot | null
  orders: OrdersState
  currency?: string
  units?: ChannelCapability['units']
  dataStale?: boolean
  refreshing?: boolean
  onClose: () => void
  onRefresh: () => Promise<void>
}

function recordNumber(value: number | null | undefined, suffix = ''): string {
  return value == null || !Number.isFinite(value) ? '未记录' : `${value.toLocaleString('zh-CN', { maximumFractionDigits: 8 })}${suffix}`
}

function timeText(value: string | null | undefined): string {
  return value?.replace('T', ' ') ?? '未记录'
}

function bookText(book: HoldingBook | null, unit: string): string {
  if (!book || book.long.volume == null || book.short.volume == null) return '待确认'
  const parts = [book.long.volume > 0 ? `多 ${sizingNumber(book.long.volume, 6)}${unit}` : '',
    book.short.volume > 0 ? `空 ${sizingNumber(book.short.volume, 6)}${unit}` : ''].filter(Boolean)
  return parts.join(' · ') || `空仓 · 0${unit}`
}

function actionText(actions: HoldingAction[] | null, unit: string, shares: boolean): string {
  if (!actions) return '待确认'
  if (!actions.length) return `0${unit}`
  return actions.map(action => `${shares ? action.kind.startsWith('买') ? '买入' : '卖出' : action.kind} ${sizingNumber(action.quantity, 6)}${unit}`).join(' · ')
}

function goalText(goal: HoldingTarget, target: TargetWeightSnapshot | null, unit: string): string {
  if (goal.quantity != null) return quantityText(goal.quantity, unit)
  if (!target?.calculated_at) return '未计算'
  return executableTargetFallbackText(target.sizing?.status, goal.evidence, unit)
}

function Explanation({ label, text }: { label: string; text: string }) {
  return <Tooltip content={text}><button type="button" aria-label={label}
    className="inline-flex size-6 flex-none cursor-help items-center justify-center rounded text-ink-3 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent">
    <Info size={14} aria-hidden />
  </button></Tooltip>
}

function Fact({ label, value, tone = 'normal', hint, compact = false }: { label: string; value: string; tone?: 'normal' | 'target' | 'warn'; hint?: string; compact?: boolean }) {
  const color = { normal: 'text-ink-1', target: 'text-accent', warn: 'text-warn' }[tone]
  return <div className={compact ? 'flex min-w-0 items-baseline gap-2' : 'min-w-0'}><dt className="text-[12px] whitespace-nowrap text-ink-2">{label}</dt>
    <dd className={`num flex min-w-0 items-center gap-1 break-words font-semibold ${compact ? 'text-[16px]' : 'mt-1 text-[15px]'} ${color}`}>
      <InkRewrite text={value} tone="label" />{hint && <Explanation label={`${label}待确认原因`} text={hint} />}
    </dd></div>
}

function Field({ label, value }: { label: string; value: string }) {
  return <div className="flex min-w-0 justify-between gap-4 py-1.5 text-[12.5px]">
    <dt className="text-ink-3">{label}</dt><dd className="num min-w-0 text-right break-words text-ink-2">{value}</dd></div>
}

function scopedProjection(props: HoldingDetailProps, symbol: string, book: HoldingBook, goal: HoldingTarget): HoldingProjection {
  const { orders } = props
  const matches = (value: string) => sameHoldingSymbol(value, symbol)
  const snapshot = orders.query.data ? { ...orders.query.data, data: orders.query.data.data.filter(order => matches(order.symbol)) } : null
  return projectHolding({ book, target: goal.quantity, orders: snapshot, receipts: orders.receipts.filter(receipt => matches(receipt.symbol)),
    observedAt: props.observedAt, stale: !!props.dataStale || orders.query.stale || !!orders.query.error })
}

function HoldingSummary({ props, book, goal, unit }: { props: HoldingDetailProps; book: HoldingBook; goal: HoldingTarget; unit: string }) {
  const actions = holdingActions(book, goal.quantity)
  return <dl className="flex min-w-0 flex-wrap items-baseline gap-x-8 gap-y-2">
    <Fact label="当前持仓" value={bookText(book, unit)} compact />
    <Fact label="目标持仓" value={goalText(goal, props.target, unit)} tone="target" compact />
    <Fact label="待调" value={props.dataStale ? '待确认' : actionText(actions, unit, props.units?.quantity_kind === 'share')} tone={actions?.length ? 'warn' : 'normal'} compact />
  </dl>
}

function HoldingBookSection({ book, unit }: { book: HoldingBook; unit: string }) {
  // 只隐藏确定为零的一侧；缺数量时保留，避免把未知持仓当成空仓。
  const sides = (['long', 'short'] as const).filter(side => book[side].volume !== 0)
  return <section aria-label="当前持仓分项">
    <h3 className="mb-2 text-[14px] font-semibold text-ink-1">当前持仓</h3>
    {!sides.length ? <p className="py-3 text-[13px] text-ink-2">当前空仓</p> : <div className="overflow-x-auto">
      <table className="num w-full min-w-[300px] table-fixed border-separate border-spacing-0 text-right text-[12.5px] [&_thead_th]:pb-1.5 [&_th]:px-3 [&_td]:px-3 [&_th:first-child]:pl-0 [&_th:last-child]:pr-0 [&_td:last-child]:pr-0">
        <colgroup>{Array.from({ length: 5 }, (_, index) => <col key={index} className="w-1/5" />)}</colgroup>
        <thead className="text-[12px] text-ink-3"><tr><th className="pb-1.5 text-left font-medium">方向</th><th className="font-medium">持仓</th><th className="font-medium">可用</th><th className="font-medium">今仓</th><th className="font-medium">昨仓</th></tr></thead>
        <tbody>{sides.map(side => <tr key={side} className="text-ink-2 [&>*]:border-t [&>*]:border-line">
          <th scope="row" className="py-1.5 text-left font-medium text-ink-1">{side === 'long' ? '多头' : '空头'}</th>
          <td className="text-ink-1">{recordNumber(book[side].volume, unit)}</td><td>{recordNumber(book[side].available, unit)}</td>
          <td>{recordNumber(book[side].today, unit)}</td><td>{recordNumber(book[side].yesterday, unit)}</td>
        </tr>)}</tbody>
      </table>
    </div>}
  </section>
}

function CalculationMetadata({ target, observedAt }: { target: TargetWeightSnapshot | null; observedAt: string | null }) {
  const quantityAt = target?.sizing?.calculated_at
  return <div className="grid gap-x-6 gap-y-1 px-[22px] py-3 text-[11.5px] text-ink-3 sm:grid-cols-2">
    <span>目标计算 {timeText(target?.calculated_at)}</span><span>持仓观测 {timeText(observedAt)}</span>
    {quantityAt && quantityAt !== target?.calculated_at && <span>数量换算 {timeText(quantityAt)}</span>}
  </div>
}

function TargetInputFields({ target, symbol, goal, unit }: { target: TargetWeightSnapshot | null; symbol: string; goal: HoldingTarget; unit: string }) {
  const evidence = goal.evidence
  const direct = evidence?.sizing_mode === 'quantity'
  const strategy = evidence?.strategy_weight ?? Object.entries(target?.strategy_weights ?? {}).find(([key]) => sameHoldingSymbol(key, symbol))?.[1]
  return <dl className="grid gap-x-6 sm:grid-cols-2">
    <Field label={direct ? '策略数量目标' : '策略目标权重'} value={direct ? quantityText(strategy, unit) : weightText(strategy)} />
    <Field label={direct ? '直接数量目标' : '账户目标权重'} value={direct ? quantityText(evidence.account_weight, unit) : weightText(goal.weight)} />
  </dl>
}

function SizingRecordFields({ evidence, unit, currency, executionId }: { evidence: TargetSizingRow; unit: string; currency: string; executionId?: string | null }) {
  const currencyUnit = displayCurrencyUnit(currency)
  const records: Array<[string, number | null, string]> = [
    ['账户乘数（含精度调整）', evidence.account_multiplier, ' 倍'], ['权重精度', evidence.weight_precision, ''],
    ['计算时账户权益', evidence.equity, ` ${currencyUnit}`], ['目标名义货值', evidence.target_notional, ` ${currencyUnit}`],
    ['参考价格', evidence.reference_price, ''], ['合约 / 单位乘数', evidence.unit_multiplier, ''],
    [`每${unit || '单位'}名义货值`, evidence.unit_notional, ` ${currencyUnit}`], ['数量步长', evidence.quantity_step, unit],
    ['最小交易数量', evidence.min_quantity, unit], ['最小名义金额', evidence.min_notional, ` ${currencyUnit}`],
  ]
  return <dl className="pt-2">
    {records.filter(([, value]) => value != null).map(([label, value, suffix]) => <Field key={label} label={label} value={recordNumber(value, suffix)} />)}
    {evidence.value_conversion && <Field label="汇率" value={recordNumber(evidence.value_conversion.rate)} />}
    {executionId && <Field label="对应执行" value={executionId} />}
  </dl>
}

function CalculationParameters({ evidence, unit, currency, executionId }: { evidence: TargetSizingRow; unit: string; currency: string; executionId?: string | null }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return <div className="mt-3 border-t border-line pt-2">
    <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(value => !value)}
      className="flex min-h-8 w-full cursor-pointer items-center justify-between text-left text-[12.5px] text-accent">
      查看计算参数<ChevronDown size={14} aria-hidden className={`transition-transform duration-200 motion-reduce:transition-none ${open ? 'rotate-180' : ''}`} />
    </button>
    <div id={id} inert={!open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden"><SizingRecordFields evidence={evidence} unit={unit} currency={currency} executionId={executionId} /></div>
    </div>
  </div>
}

function CalculationStatus({ target, goal }: { target: TargetWeightSnapshot | null; goal: HoldingTarget }) {
  const sizing = target?.sizing
  if (sizing?.status !== 'available') {
    const unavailable = sizing?.status ? sizingAvailabilityText(sizing.status) : '该次目标未保存数量换算依据'
    return <p className="mt-3 text-[13px] text-ink-2">{unavailable}</p>
  }
  if (!goal.evidence) return <p className="mt-3 text-[13px] text-ink-2">{goal.weight === 0 ? '目标为空仓' : '该品种未记录换算过程'}</p>
  return null
}

function CalculationFlow({ evidence, unit, currency }: { evidence: TargetSizingRow; unit: string; currency: string }) {
  const formulas = sizingFormulas(evidence, unit, currency)
  return <ol className="mt-3 space-y-2.5">
    {formulas.map((formula, index) => <li key={formula} className={`flex items-baseline gap-3 rounded-lg px-3 py-2.5 text-[13.5px] leading-relaxed ${index === formulas.length - 1 ? 'border border-accent/20 bg-accent/5 font-medium text-accent' : 'bg-fill text-ink-2'}`}>
      <span aria-hidden className="num text-[11px] text-ink-3">{index + 1}</span><span className="num min-w-0">{formula}</span>
    </li>)}
  </ol>
}

function TargetCalculationSection({ target, symbol, goal, unit, currency }: { target: TargetWeightSnapshot | null; symbol: string; goal: HoldingTarget; unit: string; currency: string }) {
  const sizing = target?.sizing
  const evidence = goal.evidence
  return <section className="min-w-0" aria-label="目标如何计算">
    <h3 className="mb-3 text-[14px] font-semibold text-ink-1">目标如何计算</h3>
    {sizing?.status === 'available' && evidence ? <CalculationFlow evidence={evidence} unit={unit} currency={currency} />
      : <TargetInputFields target={target} symbol={symbol} goal={goal} unit={unit} />}
    <CalculationStatus target={target} goal={goal} />
    {evidence?.status === 'UNAVAILABLE' && <p className="mt-2 text-[13px] text-warn">{sizingReasonText(evidence, unit)}</p>}
    {evidence && <CalculationParameters key={`${symbol}:${sizing?.execution_id}`} evidence={evidence} unit={unit} currency={currency} executionId={sizing?.execution_id} />}
  </section>
}

function PendingForecast({ projection, unit, shares }: { projection: HoldingProjection; unit: string; shares: boolean }) {
  const warning = projection.effect === 'overshoot' ? '在途成交后将越过目标' : projection.effect === 'farther' ? '在途成交后将扩大差额' : null
  return <div className="mb-3 rounded-lg bg-fill p-3">
    <dl className="grid grid-cols-2 gap-4">
      <Fact label="在途全部成交后" value={bookText(projection.book, unit)} hint={projection.reason ?? undefined} />
      <Fact label="预计仍差" value={actionText(projection.actions, unit, shares)} tone={projection.actions?.length ? 'warn' : 'normal'} />
    </dl>
    {warning && <p className="mt-2 text-[12.5px] text-warn">{warning}</p>}
  </div>
}

function PendingOrdersSection({ props, symbol, book, goal, unit }: { props: HoldingDetailProps; symbol: string; book: HoldingBook; goal: HoldingTarget; unit: string }) {
  const hasOrders = props.orders.query.data?.data.some(order => sameHoldingSymbol(order.symbol, symbol))
  return <section className="mt-3 border-t border-line pt-3" aria-label="该品种在途委托">
    <h3 className="mb-3 text-[14px] font-semibold text-ink-1">在途委托</h3>
    {hasOrders && <PendingForecast projection={scopedProjection(props, symbol, book, goal)} unit={unit} shares={props.units?.quantity_kind === 'share'} />}
    <CurrentOrdersContent state={props.orders} units={props.units} symbolFilter={value => sameHoldingSymbol(value, symbol)} compact />
  </section>
}

/** 横向解释窗：上方数量摘要，左侧冻结计算依据，右侧当前持仓及在途委托。 */
export function HoldingDetailDialog(props: HoldingDetailProps) {
  const { symbol, onClose, onRefresh, refreshing = false, orders } = props
  const code = symbol ?? ''
  const unit = props.units?.quantity_label ?? ''
  const book = holdingBook(props.positions, code, props.observed)
  const goal = holdingTarget(props.target, code)
  return <CenteredModal open={symbol != null} title={`${code} · 持仓与目标`} onClose={onClose} restoreFocus="keyboard" widthClassName="w-[1200px]"
    footer={<div className="border-t border-line"><CalculationMetadata target={props.target} observedAt={props.observedAt} /></div>}>
    <div className="mb-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-b border-line pb-3">
      <HoldingSummary props={props} book={book} goal={goal} unit={unit} />
      <div className="flex items-center gap-2">
      {props.dataStale && <Tooltip content="持仓或目标读取失败，当前显示最后一次成功记录。"><span tabIndex={0} className="text-[12px] text-warn">缓存数据</span></Tooltip>}
      <button type="button" aria-label="刷新持仓快照与委托" onClick={() => void onRefresh()} disabled={refreshing || orders.query.refreshing || orders.query.loading}
        className="flex min-h-8 cursor-pointer items-center gap-1.5 text-[12.5px] text-accent disabled:cursor-default disabled:text-ink-3"><RefreshCw size={14} aria-hidden />刷新</button>
      </div>
    </div>
    <div className="grid min-w-0 grid-cols-1 gap-6 pb-4 min-[900px]:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <TargetCalculationSection target={props.target} symbol={code} goal={goal} unit={unit} currency={props.currency ?? ''} />
      <div className="min-w-0 border-t border-line pt-4 min-[900px]:border-t-0 min-[900px]:border-l min-[900px]:pt-0 min-[900px]:pl-6">
        <HoldingBookSection book={book} unit={unit} />
        <PendingOrdersSection props={props} symbol={code} book={book} goal={goal} unit={unit} />
      </div>
    </div>
  </CenteredModal>
}
