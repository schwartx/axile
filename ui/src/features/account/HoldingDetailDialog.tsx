import { RefreshCw } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { CurrentOrdersContent } from '@/features/account/CurrentOrders'
import { holdingActions, holdingBook, holdingTarget, projectHolding, sameHoldingSymbol, type HoldingAction, type HoldingBook, type HoldingProjection, type HoldingTarget } from '@/features/account/holdingDetailModel'
import { SizingEvidence } from '@/features/account/SizingEvidence'
import { executableTargetFallbackText, quantityText, sizingAvailabilityText, sizingNumber, sizingReasonText, weightText } from '@/features/account/sizingEvidenceModel'
import type { OrdersState } from '@/features/account/useCurrentOrders'
import { displayCurrencyUnit } from '@/lib/format'
import type { ChannelCapability, Position, TargetSizingRow, TargetWeightSnapshot } from '@/types/api'

interface HoldingDetailProps {
  symbol: string | null
  accountName: string
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
  if (!actions.length) return '已到位'
  return actions.map(action => `${shares ? action.kind.startsWith('买') ? '买入' : '卖出' : action.kind} ${sizingNumber(action.quantity, 6)}${unit}`).join(' · ')
}

function goalText(goal: HoldingTarget, target: TargetWeightSnapshot | null, unit: string): string {
  if (goal.quantity != null) return quantityText(goal.quantity, unit)
  if (!target?.calculated_at) return '未计算'
  return executableTargetFallbackText(target.sizing?.status, goal.evidence, unit)
}

function Fact({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  return <div className="min-w-0"><dt className="text-[12px] text-ink-3">{label}</dt>
    <dd className={`num mt-1 break-words text-[15px] font-medium ${warn ? 'text-warn' : 'text-ink-1'}`}><InkRewrite text={value} tone="label" /></dd></div>
}

function Field({ label, value }: { label: string; value: string }) {
  return <div className="flex min-w-0 justify-between gap-4 py-1.5 text-[13px]">
    <dt className="text-ink-3">{label}</dt><dd className="num min-w-0 text-right break-words text-ink-2">{value}</dd></div>
}

function scopedProjection(props: HoldingDetailProps, symbol: string, book: HoldingBook, goal: HoldingTarget): HoldingProjection {
  const { orders } = props
  const matches = (value: string) => sameHoldingSymbol(value, symbol)
  const snapshot = orders.query.data ? { ...orders.query.data, data: orders.query.data.data.filter(order => matches(order.symbol)) } : null
  return projectHolding({ book, target: goal.quantity, orders: snapshot, receipts: orders.receipts.filter(receipt => matches(receipt.symbol)),
    observedAt: props.observedAt, stale: !!props.dataStale || orders.query.stale || !!orders.query.error })
}

function ProjectionNote({ projection }: { projection: HoldingProjection }) {
  if (projection.reason) return <p role="status" className="mt-1 text-[12.5px] text-ink-3">{projection.reason}，预计持仓与剩余差额待确认。</p>
  const warning = projection.effect === 'overshoot' ? '在途委托全部成交后会越过目标，需要核对超出的持仓。'
    : projection.effect === 'farther' ? '在途委托全部成交后会扩大与目标的差额。' : null
  return warning ? <p role="status" className="mt-1 text-[13px] text-warn">{warning}</p> : null
}

function HoldingSummary({ props, symbol, book, goal, unit }: {
  props: HoldingDetailProps; symbol: string; book: HoldingBook; goal: HoldingTarget; unit: string
}) {
  const projection = scopedProjection(props, symbol, book, goal)
  const actions = holdingActions(book, goal.quantity)
  const shares = props.units?.quantity_kind === 'share'
  return <>
    {props.dataStale && <p role="status" className="mb-3 text-[13px] text-warn">持仓或目标读取失败，当前展示最后一次成功记录；数量结论待确认。</p>}
    <dl className="grid grid-cols-2 gap-x-5 gap-y-4 rounded-lg border border-line p-4 sm:grid-cols-4">
      <Fact label="当前持仓" value={bookText(book, unit)} />
      <Fact label="目标持仓" value={goalText(goal, props.target, unit)} />
      <Fact label="在途全部成交后" value={bookText(projection.book, unit)} />
      <Fact label="预计仍差" value={actionText(projection.actions, unit, shares)} warn={!!projection.actions?.length} />
    </dl>
    <p className="mt-3 text-[13px] text-ink-2">当前持仓差额：{props.dataStale ? '待确认' : actionText(actions, unit, shares)}。</p>
    <ProjectionNote projection={projection} />
    <p className="mt-1 text-[12px] text-ink-3">只计入委托的剩余未成交数量。数量差额不代表可立即下单，实际执行还受可用持仓与交易规则影响。</p>
  </>
}

function HoldingBookSection({ book, unit }: { book: HoldingBook; unit: string }) {
  return <section className="mt-5 border-t border-line pt-4" aria-label="当前持仓分项">
    <h3 className="mb-2 text-[14px] font-semibold">当前持仓分项</h3>
    <div className="overflow-x-auto"><table className="num w-full min-w-[360px] text-right text-[13px]">
      <thead className="text-[12px] text-ink-3"><tr><th className="py-2 text-left font-medium">方向</th><th className="font-medium">持仓</th><th className="font-medium">可用</th><th className="font-medium">今仓</th><th className="font-medium">昨仓</th></tr></thead>
      <tbody>{(['long', 'short'] as const).map(side => <tr key={side} className="border-t border-line text-ink-2">
        <th scope="row" className="py-2 text-left font-medium">{side === 'long' ? '多头' : '空头'}</th>
        <td>{recordNumber(book[side].volume, unit)}</td><td>{recordNumber(book[side].available, unit)}</td>
        <td>{recordNumber(book[side].today, unit)}</td><td>{recordNumber(book[side].yesterday, unit)}</td>
      </tr>)}</tbody>
    </table></div>
  </section>
}

function CalculationMetadata({ target }: { target: TargetWeightSnapshot | null }) {
  const sizing = target?.sizing
  const source = target?.source === 'execution' ? '执行计算' : target?.source === 'manual' ? '手动计算' : '来源未记录'
  return <div className="mt-1 space-y-1 text-[12px] text-ink-3">
    <p>目标权重更新于 {timeText(target?.calculated_at)} · {source}</p>
    <p>数量换算记录于 {timeText(sizing?.calculated_at)}</p>
    {sizing?.execution_id && <p className="break-all">对应执行：{sizing.execution_id}</p>}
  </div>
}

function TargetInputFields({ target, symbol, goal, unit }: {
  target: TargetWeightSnapshot | null; symbol: string; goal: HoldingTarget; unit: string
}) {
  const evidence = goal.evidence
  const direct = evidence?.sizing_mode === 'quantity'
  const strategy = evidence?.strategy_weight ?? Object.entries(target?.strategy_weights ?? {}).find(([key]) => sameHoldingSymbol(key, symbol))?.[1]
  return <dl className="mt-3 grid gap-x-6 sm:grid-cols-2">
    <Field label={direct ? '策略数量目标' : '策略目标权重'} value={direct ? quantityText(strategy, unit) : weightText(strategy)} />
    <Field label={direct ? '直接数量目标' : '账户目标权重'} value={direct ? quantityText(evidence.account_weight, unit) : weightText(goal.weight)} />
  </dl>
}

function SizingRecordFields({ evidence, unit, currency, units }: {
  evidence: TargetSizingRow; unit: string; currency: string; units?: ChannelCapability['units']
}) {
  const currencyUnit = displayCurrencyUnit(currency)
  const fields = [
    ['账户乘数（含精度调整）', recordNumber(evidence.account_multiplier, ' 倍')],
    ['权重精度', recordNumber(evidence.weight_precision)],
    ['计算时账户权益', recordNumber(evidence.equity, ` ${currencyUnit}`)],
    ['目标名义货值', recordNumber(evidence.target_notional, ` ${currencyUnit}`)],
    ['计算参考价格', recordNumber(evidence.reference_price, units?.price_label ? ` ${units.price_label}` : '')],
    ['合约 / 单位乘数', recordNumber(evidence.unit_multiplier)],
    [`每${unit || '单位'}名义货值`, recordNumber(evidence.unit_notional, ` ${currencyUnit}`)],
    ['数量步长', recordNumber(evidence.quantity_step, unit)],
    ['最小交易数量', recordNumber(evidence.min_quantity, unit)],
    ['最小名义金额', recordNumber(evidence.min_notional, ` ${currencyUnit}`)],
  ]
  if (evidence.value_conversion) {
    const conversion = evidence.value_conversion
    fields.push(['价格币种 → 账户币种', `${conversion.price_currency} → ${conversion.value_currency} · 汇率 ${recordNumber(conversion.rate)}`])
  }
  return <dl className="grid gap-x-6 sm:grid-cols-2">{fields.map(([label, value]) => <Field key={label} label={label} value={value} />)}</dl>
}

function CalculationStatus({ target, goal }: { target: TargetWeightSnapshot | null; goal: HoldingTarget }) {
  const sizing = target?.sizing
  if (sizing?.status !== 'available') {
    const unavailable = sizing?.status ? sizingAvailabilityText(sizing.status) : '该次目标未保存数量换算依据'
    return <p className="mt-3 text-[13px] text-ink-3">{unavailable}。保留已知目标权重，不使用当前行情倒推当时数量。</p>
  }
  if (!goal.evidence) return <p className="mt-3 text-[13px] text-ink-3">该品种未记录换算过程{goal.weight === 0 ? '；当前目标为空仓' : ''}。</p>
  return null
}

function TargetCalculationSection({ target, symbol, goal, unit, currency, units }: {
  target: TargetWeightSnapshot | null; symbol: string; goal: HoldingTarget; unit: string; currency: string; units?: ChannelCapability['units']
}) {
  const sizing = target?.sizing
  const evidence = goal.evidence
  return <section className="mt-5 border-t border-line pt-4" aria-label="目标如何计算">
    <h3 className="text-[14px] font-semibold">目标如何计算</h3>
    <CalculationMetadata target={target} />
    <TargetInputFields target={target} symbol={symbol} goal={goal} unit={unit} />
    {evidence && <>
      <SizingRecordFields evidence={evidence} unit={unit} currency={currency} units={units} />
      {sizing?.status === 'available' && <div className="mt-2 rounded-lg bg-fill px-3 py-2">
        <SizingEvidence key={`${symbol}:${sizing.execution_id}:${sizing.calculated_at}`} row={evidence} quantityLabel={unit} currency={currency} defaultExpanded />
      </div>}
      {evidence.status === 'UNAVAILABLE' && <p className="mt-2 text-[13px] text-warn">{sizingReasonText(evidence, unit)}</p>}
    </>}
    <CalculationStatus target={target} goal={goal} />
  </section>
}

/** 单品种解释窗只读已保存的目标依据，与账户委托窗共享查询和人工撤单状态。 */
export function HoldingDetailDialog(props: HoldingDetailProps) {
  const { symbol, accountName, onClose, onRefresh, refreshing = false, orders, observedAt } = props
  const code = symbol ?? ''
  const unit = props.units?.quantity_label ?? ''
  const book = holdingBook(props.positions, code, props.observed)
  const goal = holdingTarget(props.target, code)
  return <CenteredModal open={symbol != null} title={`${code} · 持仓与目标`} subtitle={accountName} onClose={onClose} widthClassName="w-[860px]"
    footer={<div className="flex justify-end border-t border-line px-[22px] py-3">
      <button type="button" onClick={onClose} className="min-h-9 cursor-pointer rounded-lg border border-line px-4 text-sm text-ink-2 hover:bg-fill">关闭</button>
    </div>}>
    <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-[12px] text-ink-3">
      <span>持仓观测于 {timeText(observedAt)}</span>
      <button type="button" aria-label="刷新持仓快照与委托" onClick={() => void onRefresh()} disabled={refreshing || orders.query.refreshing || orders.query.loading}
        className="flex min-h-8 cursor-pointer items-center gap-1.5 text-accent disabled:cursor-default disabled:text-ink-3">
        <RefreshCw size={14} aria-hidden />刷新快照与委托
      </button>
    </div>
    <HoldingSummary props={props} symbol={code} book={book} goal={goal} unit={unit} />
    <HoldingBookSection book={book} unit={unit} />
    <TargetCalculationSection target={props.target} symbol={code} goal={goal} unit={unit} currency={props.currency ?? ''} units={props.units} />
    <section className="mt-5 border-t border-line pt-4" aria-label="该品种在途委托">
      <h3 className="mb-2 text-[14px] font-semibold">在途委托</h3>
      <CurrentOrdersContent state={orders} units={props.units} symbolFilter={value => sameHoldingSymbol(value, code)} />
    </section>
  </CenteredModal>
}
