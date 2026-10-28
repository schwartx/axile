import { displayCurrencyUnit } from '@/lib/format'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ExternalLink, X } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { amount, coverageText, feeText, quantityText } from '@/features/history/costs'
import { executionRecordView } from '@/features/account/executionOutcome'
import { quantityUnit } from '@/features/history/executionEvidenceModel'
import { xPosition, type ChartScene } from '@/components/viz/performanceCanvas'
import type { ChartSelection } from '@/features/history/chartModel'
import { timeLabel } from '@/features/history/chartModel'
import { useAnalysisExecutionDetail } from '@/features/history/useAnalysisExecutionDetail'
import { checkPerformance } from '@/features/history/performanceCache'
import type { DetailTransaction, ExecutionEvidence } from '@/features/history/analysisExecutionDetail'
import type { AnalysisExecutionSummary } from '@/lib/api/performance'
import { ApiError } from '@/lib/api/client'
import type { ChannelCapability } from '@/types/api'

interface Cursor { time: number; x: number; y: number; recordId?: number }
interface Props {
  scene: ChartScene; rows: AnalysisExecutionSummary[]; snapshotId: string; accountId: number; currency: string; units?: ChannelCapability['units']
  cursor: Cursor | null; selection: ChartSelection; onSelect: (value: ChartSelection) => void
  onInspect: (cursor: Cursor | null) => void
}

const focusClass = 'rounded focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
const nullableTime = (time: number | null) => time == null ? '时间未知' : timeLabel(time)
function positionDirection(direction: string | null, quantity: number | null): string {
  if (quantity != null && quantity < 0 || direction != null && /short|空/i.test(direction)) return '空'
  if (direction != null && /long|多/i.test(direction)) return '多'
  return direction || '方向未知'
}
function stateLabel(row: AnalysisExecutionSummary): string {
  return row.noop ? '无需交易' : row.is_success === 1 ? '执行成功' : row.is_success === 0 ? '执行未成功' : '执行状态未知'
}
function hoverExecution(rows: AnalysisExecutionSummary[], cursor: Cursor | null): AnalysisExecutionSummary | null {
  if (!cursor) return null
  if (cursor.recordId != null) return rows.find(row => row.record_id === cursor.recordId) ?? null
  let latest: AnalysisExecutionSummary | null = null
  for (const row of rows) {
    if (row.time_ms <= cursor.time && (!latest || row.time_ms > latest.time_ms || row.time_ms === latest.time_ms && row.record_id > latest.record_id)) latest = row
  }
  return latest
}

function PositionList({ detail, units, currency }: { detail: ExecutionEvidence; units?: ChannelCapability['units']; currency: string }) {
  const positions = detail.positions
  return <div className="mt-2 border-t border-line pt-2">
    <div className="mb-1 text-ink-3">执行后持仓</div>
    {positions == null ? <p>持仓状态未知</p> : positions.length === 0 ? <p>{detail.positionsComplete ? '空仓' : '持仓明细尚未加载完整'}</p> : <>
      {positions.map(p => <div key={p.idx} className="flex justify-between gap-3 py-0.5"><span>{p.symbol ?? '品种未知'} · {positionDirection(p.direction, p.quantity)}</span><span>{quantityText(p.quantity == null ? null : Math.abs(p.quantity))} {p.symbol == null ? '' : quantityUnit(units, p.symbol, currency)}</span></div>)}
      {!detail.positionsComplete && <p className="mt-1 text-ink-3">持仓明细尚未加载完整</p>}
    </>}
  </div>
}

function afterPosition(trade: DetailTransaction, detail: ExecutionEvidence, qty: (value: number | null) => string): string {
  if (detail.positions == null) return '未知'
  if (!detail.positionsComplete) return '持仓明细尚未加载完整'
  const positions = detail.positions.filter(position => position.symbol === trade.symbol)
  if (!positions.length) return qty(0)
  return positions.map(position => `${positionDirection(position.direction, position.quantity)} ${qty(position.quantity == null ? null : Math.abs(position.quantity))}`).join(' / ')
}

function Transaction({ trade, detail, currency, units }: { trade: DetailTransaction; detail: ExecutionEvidence; currency: string; units?: ChannelCapability['units'] }) {
  const qty = (value: number | null) => `${quantityText(value)} ${trade.symbol == null ? '' : quantityUnit(units, trade.symbol, currency)}`
  const loss = trade.summary.lossBp
  const side = trade.side === 'buy' ? '买入' : trade.side === 'sell' ? '卖出' : '方向未知'
  const partial = trade.summary.covered != null && trade.summary.count != null && trade.summary.covered < trade.summary.count
  const fee = Object.entries(trade.summary.fees).map(([unit, value]) => `${amount(value)} ${unit}`).join(' · ')
  const reference = trade.referenceSource === 'last' ? '参考最新价' : trade.referenceSource === 'mid' ? '参考中间价' : '参考价来源未知'
  return <div className="border-t border-line py-2">
    <div className="flex flex-wrap justify-between gap-x-3 gap-y-1 font-medium"><span>{trade.symbol ?? '品种未知'}</span><span>{side} {qty(trade.quantity)}</span></div>
    <div className="mt-1 flex flex-wrap justify-between gap-x-3 text-ink-2"><span>均价 {amount(trade.price)}</span><span className={loss == null || loss === 0 ? 'text-ink-3' : loss > 0 ? 'text-warn' : 'text-accent'}>{loss == null ? '滑点未知' : `${loss > 0 ? '不利' : loss < 0 ? '有利' : '持平'} ${amount(Math.abs(loss))} BP`} · {partial ? '已知成本 ' : '成本 '}{amount(trade.summary.cost)} {displayCurrencyUnit(currency)}</span></div>
    <div className="mt-1 space-y-1 text-ink-3"><p>原持仓 {qty(trade.before)} → 执行后 {afterPosition(trade, detail, qty)}</p><p>目标持仓 {qty(trade.target)} · {reference} {amount(trade.reference)}</p><p>{nullableTime(trade.time)}{trade.endTime != null && trade.endTime !== trade.time ? ` → ${nullableTime(trade.endTime)}` : ''}{trade.timeEstimated ? '（成交时间缺失，使用执行时间）' : ''}</p><p>手续费 {fee || '—'}</p>{partial && <p>滑点仅覆盖 {trade.summary.covered}/{trade.summary.count} 笔成交</p>}</div>
  </div>
}

function detailError(error: Error, expired: boolean): string {
  if (expired) return '该版本已过期，请重新读取绩效页后再查看详情'
  if (error instanceof ApiError && error.status === 409) return '该版本没有可读取的明细视图，请重新计算分析结果'
  if (error instanceof ApiError && error.status === 404) return '该版本中没有这次执行的明细'
  return `明细读取失败：${error.message}`
}

export function ChartTradingOverlay({ scene, rows, snapshotId, accountId, currency, units, cursor, selection, onSelect, onInspect }: Props) {
  const [positionsKey, setPositionsKey] = useState<string | null>(null)
  const pinned = selection?.kind === 'execution' ? rows.find(row => row.record_id === selection.recordId) ?? null : null
  const row = pinned ?? hoverExecution(rows, cursor)
  const details = useAnalysisExecutionDetail(accountId, snapshotId, pinned?.record_id ?? null)
  const detail = details.data
  const cardRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)
  const selectedKey = pinned ? `${snapshotId}:${pinned.record_id}` : null
  const recoveredVersions = useRef(new Set<string>())
  useEffect(() => {
    if (!details.expired || recoveredVersions.current.has(snapshotId)) return
    recoveredVersions.current.add(snapshotId)
    void checkPerformance(accountId, scene.data.range)
  }, [details.expired, snapshotId, accountId, scene.data.range])
  const showPositions = selectedKey != null && selectedKey === positionsKey
  useLayoutEffect(() => {
    if (!selectedKey) return
    const previousFocus = document.activeElement
    const card = cardRef.current
    const chart = card?.parentElement?.parentElement?.querySelector<HTMLElement>('canvas[tabindex="0"]') ?? null
    if (!card?.contains(previousFocus)) returnFocusRef.current = previousFocus instanceof HTMLElement && previousFocus !== document.body ? previousFocus : chart
    const returnFocus = returnFocusRef.current
    closeRef.current?.focus({ preventScroll: true })
    return () => {
      // React may detach the focused card before its parent's layout cleanup.
      if ((card?.contains(document.activeElement) || document.activeElement === document.body) && returnFocus?.isConnected) returnFocus.focus({ preventScroll: true })
    }
  }, [selectedKey])
  const visible = !!pinned || !!cursor
  const anchor = pinned && selection?.kind === 'execution' ? { x: xPosition(selection.time, scene.width, scene.viewport, scene.scale), y: 70 } : cursor ?? { x: 20, y: 20 }
  const width = Math.max(0, Math.min(370, scene.width - 16))
  const left = Math.max(8, Math.min(scene.width - width - 8, anchor.x > scene.width / 2 ? anchor.x - width - 20 : anchor.x + 20))
  const close = () => {
    // Restore before removing the focused button; otherwise the browser focuses body.
    if (returnFocusRef.current?.isConnected) returnFocusRef.current.focus({ preventScroll: true })
    onInspect(null); onSelect(null)
  }
  const title = pinned ? `${timeLabel(pinned.time_ms)} · ${detail?.symbolsComplete ? executionRecordView(detail.rawResult).text : stateLabel(pinned)}` : cursor ? timeLabel(cursor.time) : ''
  const executionId = detail?.executionId ?? pinned?.execution_id
  return <div className="pointer-events-none absolute inset-0" data-testid="chart-trading-overlay">
    {visible && <div ref={cardRef} data-testid="chart-trade-card" role={pinned ? 'dialog' : 'tooltip'} aria-label={pinned ? '成交与持仓详情' : '执行摘要预览'} aria-modal={pinned ? false : undefined} className={`${pinned ? 'pointer-events-auto' : 'pointer-events-none'} absolute z-20 overflow-y-auto overscroll-contain rounded-md border border-line bg-surface px-3 py-2.5 text-xs text-ink-1 shadow-md`} style={{ left, top: Math.max(8, Math.min(90, anchor.y - 55)), width, maxHeight: pinned ? 400 : 340 }} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); close() } }}>
      <div className="flex items-start justify-between gap-2"><div className="font-medium">{title}</div>
        {pinned && <button ref={closeRef} type="button" aria-label="关闭成交浮层" className={`-mr-1 -mt-1 flex h-7 w-7 shrink-0 items-center justify-center hover:bg-fill ${focusClass}`} onClick={close}><X size={15} /></button>}
      </div>
      {row && <div className="mt-2 space-y-1 text-ink-2"><p>{row.summary.covered < row.summary.count ? '已知成本' : '成本'} {amount(row.summary.cost)} {displayCurrencyUnit(currency)} · {coverageText(row.summary)}</p><p>手续费 {feeText(row.summary)}</p>{row.reason && <p className="text-warn">{row.reason}</p>}</div>}
      {pinned && <>
        {(details.loading || !detail && !details.error) && <p role="status" className="mt-2 text-ink-3">{detail ? '正在读取更多明细…' : '正在读取本次执行详情…'}</p>}
        {details.error && <div className="mt-2 text-warn"><p role="alert">{detailError(details.error, details.expired)}</p>{details.expired && <button type="button" className={`mt-1 min-h-9 text-accent ${focusClass}`} onClick={() => void checkPerformance(accountId, scene.data.range)}>重新读取绩效</button>}{!details.expired && !(details.error instanceof ApiError && [404, 409].includes(details.error.status)) && <button type="button" className={`mt-1 min-h-9 text-accent ${focusClass}`} onClick={() => void details.retry()}>重试明细</button>}</div>}
        {detail && <>
          {detail.reason && detail.reason !== row?.reason && <p className="mt-2 text-warn">{detail.reason}</p>}
          {detail.attempts.map(attempt => <div key={attempt.idx} className="mt-2 border-t border-line pt-2"><div>{attempt.symbol ?? '品种未知'} · 计划净交易 {quantityText(attempt.planned)} / 实际净成交 {quantityText(attempt.filled)} {attempt.symbol == null ? '' : quantityUnit(units, attempt.symbol, currency)}</div>{attempt.reason && <p className="mt-1 text-warn">{attempt.reason}</p>}</div>)}
          {detail.transactions.map(trade => <Transaction key={trade.idx} trade={trade} detail={detail} currency={currency} units={units} />)}
          {detail.transactions.length === 0 && <p className="mt-2 text-ink-3">{pinned.noop ? '本次无需交易' : '无可用成交明细'}</p>}
          <p className="mt-2 text-ink-3">已加载 {detail.transactions.length}/{detail.transactionTotal} 项成交明细{detail.nextOffset != null ? ' · 还有明细未加载' : ''}</p>
          {detail.nextOffset != null && !details.expired && <button type="button" disabled={details.loading} className={`min-h-9 text-accent disabled:text-ink-3 ${focusClass}`} onClick={() => void details.loadMore()}>加载更多明细</button>}
          <div inert={!showPositions} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${showPositions ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden"><PositionList detail={detail} units={units} currency={currency} /></div></div>
        </>}
        <div className="sticky -bottom-2.5 -mx-3 mt-2 flex flex-wrap items-center gap-x-3 border-t border-line bg-surface px-3 pt-1">
          {detail && <button type="button" aria-expanded={showPositions} className={`min-h-9 text-accent ${focusClass}`} onClick={() => setPositionsKey(showPositions ? null : selectedKey)}>{showPositions ? '收起持仓' : '持仓明细'}</button>}
          {executionId && <Link to={`/accounts/${accountId}/executions/${executionId}`} state={{ performanceReturn: `/accounts/${accountId}/history` }} className={`inline-flex min-h-9 items-center gap-1 text-accent ${focusClass}`}>执行详情 <ExternalLink size={12} /></Link>}
        </div>
      </>}
      {!pinned && <p className="mt-2 text-[10px] text-ink-3">{row ? '最近执行摘要 · 点击固定后读取成交与持仓' : '点击执行标记查看成交与持仓'}</p>}
    </div>}
  </div>
}
