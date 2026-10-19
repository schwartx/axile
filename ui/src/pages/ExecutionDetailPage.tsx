import { isDegradedSnapshotSource } from '@/features/account/accountSnapshot'
import { lossClass } from '@/features/history/costs'
import { useCallback } from 'react'
import { useLocation, useParams } from 'react-router'
import { Card } from '@/components/ui/Card'
import { SkeletonLines } from '@/components/ui/Skeleton'
import { NumberTicker } from '@/components/ui/NumberTicker'
import { ExecutionErrorEvidence } from '@/features/history/ExecutionErrorEvidence'
import { readEvidencePages, type EvidencePage } from '@/features/history/executionErrorAdapter'
import { ErrorCopyButton } from '@/components/ui/ErrorDetailsContent'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { SizingEvidence } from '@/features/account/SizingEvidence'
import { weightText } from '@/features/account/sizingEvidenceModel'
import { usePolling } from '@/lib/hooks/usePolling'
import { getExecutionArtifacts, getExecutionEvents, getExecutionStatus } from '@/lib/api/executions'
import { shortErrorReason } from '@/lib/errorInfo'
import { currencyOf } from '@/lib/derive'
import { displayCurrencyUnit, fmtBps, fmtMoney, withCurrency } from '@/lib/format'
import {
  buildExecutionDetail,
  executionHeadline,
  formatOrderTradeCounts,
  SOURCE_DEGRADED_LABEL,
  type ChainAction,
  type ExecutionDetailModel,
  type SymbolChain,
} from '@/features/account/executionDetail'
import { buildSymbolActionStream, type ActionLine } from '@/features/account/actionStream'
import { executionRecordView } from '@/features/account/executionOutcome'
import { AccountPageTitle } from '@/features/account/pageHead'
import { EvidenceExportButton } from '@/features/account/EvidenceExportButton'
import type { FailureReason } from '@/features/account/failureReason'
import { PhaseBar } from '@/features/dashboard/PhaseBar'
import { accountAssetTerms, positionValueLabelOf } from '@/features/dashboard/display'
import { useDomainStore } from '@/stores/domain'
import { useChannelDescriptor } from '@/stores/channels'
import { useRunning } from '@/stores/liveExec'
import type { ChannelCapability, ExecOrder, ExecutionArtifact, ExecutionEvent, ExecutionStatus } from '@/types/api'

/** 触发来源文案。 */
const TRIGGER_LABEL: Record<string, string> = { manual: '手动', scheduler: '定时', empty_positions: '清仓' }
/** 主/被动成交文案。 */
const LIQUIDITY_LABEL: Record<string, string> = { passive: '被动', aggressive: '主动', mixed: '混合' }
/** 订单类型文案。 */
const ORDER_TYPE_LABEL: Record<string, string> = { LIMIT: '限价', MARKET: '市价' }
/** 执行类型文案。 */
const KIND_LABEL: Record<string, string> = { rebalance: '调仓', clear_positions: '清仓' }
/** 逐只动作标签。 */
const ACTION_LABEL: Record<ChainAction, string> = {
  reduce: '减仓',
  increase: '加仓',
  open: '建仓',
  close: '清仓',
  flip: '翻向',
  aligned: '到位',
  skipped: '跳过',
  failed: '失败',
}
/**
 * 脊柱节点圆点着色。
 *
 * 三族分明、皆不碰红绿（红绿专供行情涨跌）：过程步=中性弱点，正向里程碑（到位/对账成功）
 * =accent 蓝给一个从容的确认，注意/错误=同一琥珀——错误的严重度改由断点行的填充/标签承载，
 * 不借「失败红」。蓝只标「干得好」，琥珀只标「要看看」，二者互不遮蔽。
 */
const DOT_CLASS: Record<string, string> = {
  INFO: 'bg-border-strong',
  SUCCESS: 'bg-accent',
  WARNING: 'bg-warn',
  ERROR: 'bg-warn',
}

type DisplayUnits = ChannelCapability['units']

const DEFAULT_UNITS: DisplayUnits = {
  quantity_kind: 'custom',
  quantity_label: '',
  quantity_max_decimals: 6,
  price_label: '',
  notional_label: '',
}

/** 持仓/成交量按渠道精度格式化，并在需要时追加单位。 */
function fmtQty(v: number, units: DisplayUnits, withUnit = true): string {
  const value = Math.abs(v).toLocaleString('zh-CN', {
    maximumFractionDigits: units.quantity_max_decimals,
  })
  return withUnit && units.quantity_label ? `${value} ${units.quantity_label}` : value
}

/** 价格按通用精度格式化，并追加渠道报价单位。 */
function fmtPrice(v: number, units: DisplayUnits, currency: string): string {
  const value = Math.abs(v).toLocaleString('zh-CN', { maximumFractionDigits: 6 })
  const label = displayCurrencyUnit(units.price_label || currency)
  return label ? `${value} ${label}` : value
}

/** 基础资产数量只显示数字，不追加标签或从品种派生币种单位。 */
function quantityDisplayUnits(units: DisplayUnits): DisplayUnits {
  return units.quantity_kind === 'base_asset' ? { ...units, quantity_label: '' } : units
}

/** 持仓与目标保留方向符号；订单成交数量仍使用绝对值。 */
function fmtPosition(v: number | null, units: DisplayUnits): string {
  return v == null ? '—' : `${v < 0 ? '−' : ''}${fmtQty(v, units)}`
}

/** 权益金额：两位小数、千分位。 */
function fmtEquity(v: number | null): string {
  return v == null ? '—' : v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 带符号权益差。 */
function fmtDelta(before: number | null, after: number | null): string {
  if (before == null || after == null) return ''
  const d = after - before
  return `${d >= 0 ? '+' : '−'}${Math.abs(d).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** 耗时秒 → `41.2s` / `1m5s`。 */
function fmtDuration(sec: number | null): string {
  if (sec == null) return ''
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  return `${m}m${Math.round(sec % 60)}s`
}

/** 敞口整数百分数。 */
function expPct(v: number | null): string {
  return v == null ? '—' : `${Math.round(v)}%`
}

/** 头条：整条链坍缩成的结论 + 诚实来源条。 */
function Header({
  m,
  currency,
  assetLabel,
}: {
  m: ExecutionDetailModel
  currency: string
  assetLabel: string
}) {
  const v = executionHeadline(m)
  const h = m.header
  const meta = [TRIGGER_LABEL[h.trigger] ?? h.trigger, KIND_LABEL[h.kind] ?? h.kind].filter(Boolean).join(' ')
  return (
    <div className="mt-3">
      <div className={`text-[16px] ${v.level === 'warn' ? 'font-semibold text-warn' : 'text-ink-1'}`}>{v.text}</div>
      <div className="num mt-2 text-[14px] text-ink-2">
        {meta && <span className="text-ink-3">{meta} · </span>}
        {h.durationSec != null && <span className="text-ink-3">{fmtDuration(h.durationSec)} · </span>}
        {assetLabel} {fmtEquity(h.equityBefore)} → {h.equityAfter == null ? '—' : <NumberTicker value={h.equityAfter} format={{ minimumFractionDigits: 2, maximumFractionDigits: 2 }} />}
        {h.equityBefore != null && h.equityAfter != null && (
          <span className={`font-semibold ${h.equityAfter === h.equityBefore ? 'text-ink-2' : h.equityAfter > h.equityBefore ? 'text-up' : 'text-down'}`}>
            {' '}
            ({fmtDelta(h.equityBefore, h.equityAfter)}) {displayCurrencyUnit(currency)}
          </span>
        )}
        {h.exposureBefore != null && (
          <span className="text-ink-3">
            {' · '}敞口 {expPct(h.exposureBefore)} → {h.exposureAfter == null ? '—' : <NumberTicker value={h.exposureAfter} format={{ maximumFractionDigits: 0 }} suffix="%" />}
          </span>
        )}
      </div>
      <SnapshotNote m={m} assetLabel={assetLabel} />
      {m.terminationWarning && (
        <div className="mt-2 rounded bg-warn-soft px-2.5 py-1.5 text-[13.5px] text-warn">
          {m.terminationWarning}
        </div>
      )}
      {m.failure && <FailureNote f={m.failure} />}
    </div>
  )
}

/**
 * 账户快照退化时说明缺失范围；逐品种先前确认的持仓仍是独立证据。
 */
function SnapshotNote({ m, assetLabel }: { m: ExecutionDetailModel; assetLabel: string }) {
  const h = m.header
  if (m.task?.status === 'FAILED' && m.task.started_at == null) return null
  const posBad = isDegradedSnapshotSource(h.sourcePosition)
  const eqBad = isDegradedSnapshotSource(h.sourceEquity)
  if (!posBad && !eqBad) return null
  const label = (s: string) => SOURCE_DEGRADED_LABEL[s] ?? s
  const note = posBad
    ? `执行后账户快照为「${label(h.sourcePosition)}」，${assetLabel}变动与敞口对比不可得；逐品种仅展示已确认的持仓证据，缺少证据时到位状态未知。`
    : `执行前基线为「${label(h.sourceEquity)}」，${assetLabel}变动与敞口对比不可得；执行后持仓与到位证据仍然保留。`
  return <div className="mt-2 rounded bg-warn-soft px-2.5 py-1.5 text-[13.5px] text-warn">{note}</div>
}

/** 直接显示已有原因，原始文本在证据区回放。 */
function FailureNote({ f }: { f: FailureReason }) {
  return <div className="my-2 rounded bg-warn-soft px-2.5 py-2 text-[14px] text-warn">{shortErrorReason(f.human)}</div>
}

/** ② 目标变化段：回答「为什么调」。 */
function TargetChangeView({ m }: { m: ExecutionDetailModel }) {
  const tc = m.targetChange
  if (!tc) return null
  return (
    <div className="mb-4">
      <div className="mb-2 text-xs font-semibold tracking-wide text-ink-3">目标变化</div>
      <div className="num text-[14px] text-ink-2">
        {tc.rows.map((r) => (
          <span key={r.symbol} className="mr-4 inline-block">
            {r.symbol}{' '}
            {r.strategy != null ? (
              <>
                <span className="text-ink-3">策略 {weightText(r.strategy)} → </span>
                <span>账户 {weightText(r.curr)}</span>
              </>
            ) : (
              <>
                {r.last != null && <span className="text-ink-3">{weightText(r.last)} → </span>}
                <span>{weightText(r.curr)}</span>
              </>
            )}
          </span>
        ))}
      </div>
      {(tc.algorithm || tc.algoParams) && (
        <div className="mt-1.5 text-[13px] text-ink-3">
          {[tc.algorithm, tc.algoParams].filter(Boolean).join(' · ')}
        </div>
      )}
      {(tc.forbidden.length > 0 || tc.risk.length > 0) && (
        <div className="mt-1 text-[13px] text-warn">
          {tc.forbidden.length > 0 && <span>禁止 {tc.forbidden.join('/')} </span>}
          {tc.risk.length > 0 && <span>风控 {tc.risk.join('/')}</span>}
        </div>
      )}
    </div>
  )
}

/** 订单 → 成交 树（默认折叠的取证下钻）。 */
function OrderTree({ orders, currency, units }: { orders: ExecOrder[]; currency: string; units: DisplayUnits }) {
  if (orders.length === 0) return null
  const nTrades = orders.reduce((n, o) => n + o.trades.length, 0)
  const hasFills = orders.some((o) => (o.filled_volume ?? 0) > 0)
  return (
    <details className="mt-1.5">
      <summary className="cursor-pointer select-none text-[12.5px] text-ink-3 hover:text-ink-2">
        订单与成交（{formatOrderTradeCounts(orders.length, nTrades, hasFills, ' · ')}）
      </summary>
      <div className="mt-1 border-l border-line pl-3">
        {orders.map((o) => (
          <div key={o.order_id} className="num py-1 text-[12.5px]">
            <div className="text-ink-2">
              {o.side === 'sell' ? '卖' : o.side === 'buy' ? '买' : ''} {ORDER_TYPE_LABEL[o.order_type] ?? o.order_type}
              {o.order_type !== 'MARKET' && o.price != null && <span> @{fmtPrice(o.price, units, currency)}</span>}
              {' · '}成交 {fmtQty(o.filled_volume ?? 0, units, false)}/{fmtQty(o.volume ?? 0, units)}
              {o.status && <span className="text-ink-3"> · {o.status}</span>}
              {o.client_order_id && <span className="text-ink-3"> · {o.client_order_id}</span>}
            </div>
            {o.trades.map((t, i) => (
              <div key={i} className="pl-3 text-ink-3">
                成交 {fmtQty(t.volume ?? 0, units)} @{fmtPrice(t.price ?? 0, units, currency)}
                {t.time && <span> · {t.time.slice(11, 19)}</span>}
              </div>
            ))}
          </div>
        ))}
      </div>
    </details>
  )
}

/** ③ 单只子链：意图 → 决策 → 成交 →（执行质量）→ 到位，可下钻因果动作流/订单成交。 */
function SymbolChainRow({
  s,
  currency,
  units,
  lines,
}: {
  s: SymbolChain
  currency: string
  units: DisplayUnits
  lines: ActionLine[]
}) {
  // 到位=accent 蓝 ✓（正向确认，蓝≠红绿故不撞涨跌）；未到位/欠量=琥珀 ⚠。
  // 到位比例随成交推进而变，用 NumberTicker 里程表式滚动（仅 isLive 轮询下值真变时才动，完成态静止）。
  const ratioNode =
    s.attainedRatio == null ? null : (
      <NumberTicker value={s.attainedRatio * 100} format={{ minimumFractionDigits: 2, maximumFractionDigits: 2 }} suffix="%" />
    )
  const result = executionRecordView({ status: s.status })
  const outcome = { cls: result.warning ? 'text-warn' : 'text-ink-3', node: result.title }
  const sideText = s.side === 'sell' ? '卖' : s.side === 'buy' ? '买' : ''
  const driftNotable = Math.abs(s.drift) > 1e-6
  // 浮盈：当前水平取 after（无则回退 before）。未成交时前→后 Δ 纯为行情漂移、与执行无关，
  // 只显当前水平、不摆箭头（不把行情盈亏栽到执行头上）；成交了才显前→后。
  const pnlLevel = s.pnlAfter ?? s.pnlBefore
  const showPnlDelta = s.filled !== 0 && s.pnlBefore != null && s.pnlAfter != null
  const tca = s.tca
  const hasQuality = tca != null && (tca.n_trades > 0 || tca.slippage_bps != null)
  const priceUnits = s.priceCurrency ? { ...units, price_label: s.priceCurrency } : units

  return (
    <div className={`border-t border-line py-3 first:border-t-0 ${s.broken ? 'bg-warn-soft/50 -mx-6 px-6' : ''}`}>
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <span className="text-[14.5px] font-medium">{s.symbol}</span>
          {s.observedBefore != null && s.observedAfter != null && <span className="text-[12px] text-ink-3">{ACTION_LABEL[s.action]}</span>}
        </div>
        <span className={`text-[14px] font-semibold ${outcome.cls}`}>{outcome.node}</span>
      </div>

      {s.sizing ? (
        <div className="mt-1">
          <SizingEvidence row={s.sizing} quantityLabel={units.quantity_label} currency={currency} />
        </div>
      ) : (
        <div className="mt-1 text-[12.5px] text-warn">当时未记录换算依据</div>
      )}
      <div className="num mt-0.5 text-[13px] text-ink-3">
        实际 {fmtPosition(s.observedBefore, units)} → {fmtPosition(s.observedAfter, units)}
        {s.target != null && <span> · 可执行目标 {fmtPosition(s.target, units)}</span>}
        {ratioNode != null && <span> · 到位比例 {ratioNode}</span>}
        {s.target != null && s.observedAfter != null && s.reached === false && <span> · 差额 {fmtPosition(s.target - s.observedAfter, units)}</span>}
      </div>

      {/* 决策 + 成交 */}
      {s.side !== 'none' && s.filled !== 0 && (
        <div className="num mt-0.5 text-[13.5px] text-ink-3">
          {s.algorithm && <span>{s.algorithm} · </span>}
          {sideText} {fmtQty(s.filled, units)}
          {s.avgPrice != null && <span> @{fmtPrice(s.avgPrice, priceUnits, currency)}</span>}
          <span className="text-ink-2">
            {' '}（≈{s.filledValue == null ? '—' : withCurrency(fmtMoney(s.filledValue), s.valueCurrency || units.notional_label || currency)}）
          </span>
          {s.terminalStatus && <span className={s.broken ? 'text-warn' : ''}> · {s.terminalStatus}</span>}
          {s.legSeconds != null && <span> · 用时 {Math.round(s.legSeconds)}s</span>}
        </div>
      )}

      {/* 执行质量（TCA）：主/被动 · 滑点 · 单/成交笔数 */}
      {hasQuality && (
        <div className="num mt-0.5 text-[13px]">
          {tca.liquidity && <span className="text-ink-2">{LIQUIDITY_LABEL[tca.liquidity] ?? tca.liquidity}</span>}
          {tca.slippage_bps != null && (
            <span className={lossClass(-tca.slippage_bps)}>
              {tca.liquidity ? ' · ' : ''}滑点损耗 {fmtBps(-tca.slippage_bps)}
            </span>
          )}
          <span className="text-ink-3">
            {tca.liquidity || tca.slippage_bps != null ? ' · ' : ''}
            {formatOrderTradeCounts(tca.n_orders, tca.n_trades, s.filled !== 0)}
            {tca.fill_ratio != null && tca.fill_ratio < 0.999 && (
              <span className="text-warn"> · 成交率 {(tca.fill_ratio * 100).toFixed(0)}%</span>
            )}
          </span>
        </div>
      )}

      {/* 订单腿原因：断点时琥珀主因（为何没到位）；已到位时降为中性注脚（受阻/空跑，安静即好）。 */}
      {s.reason && (
        <div className={`mt-0.5 text-[13px] ${s.broken ? 'font-medium text-warn' : 'text-ink-3'}`}>{s.reason}</div>
      )}

      {/* 浮盈 / drift */}
      {(pnlLevel != null || driftNotable) && (
        <div className="num mt-0.5 text-[12.5px] text-ink-3">
          {pnlLevel != null && (
            <span>浮盈 {showPnlDelta ? `${s.pnlBefore!.toFixed(1)} → ${s.pnlAfter!.toFixed(1)}` : pnlLevel.toFixed(1)}</span>
          )}
          {driftNotable && <span className="text-warn"> · 偏差 {s.drift.toFixed(4)}</span>}
        </div>
      )}

      {/* 下钻：因果动作流（主）→ 订单成交（取证） */}
      <SymbolActionStream lines={lines} orders={s.orders} currency={currency} units={priceUnits} />
    </div>
  )
}

/** ④ 外层控制面骨干（生命周期，细框、带时序、断点变红）——跨品种的两阶段门控在此。 */
function Spine({ m }: { m: ExecutionDetailModel }) {
  if (m.spine.length === 0) return null
  return (
    <div className="mt-5">
      <div className="mb-2 text-xs font-semibold tracking-wide text-ink-3">执行链路</div>
      <div className="relative pl-[22px]">
        <div className="absolute left-1.5 top-1.5 bottom-1.5 w-0.5 bg-line" />
        {m.spine.map((n) => (
          <div key={n.key} className="relative flex items-baseline gap-3 py-1.5">
            <span
              className={`absolute -left-[17px] top-[9px] h-[9px] w-[9px] rounded-full border-2 border-surface ${DOT_CLASS[n.status] ?? DOT_CLASS.INFO}`}
            />
            {n.time && <span className="num w-20 flex-none text-[12px] text-ink-3">{n.time}</span>}
            {!n.time && <span className="w-20 flex-none" />}
            <span className={`text-[14px] ${n.broken ? 'font-medium text-warn' : 'text-ink-2'}`}>{n.label}</span>
            {n.detail && <span className="num text-[13px] text-ink-3">{n.broken ? shortErrorReason(n.detail) : n.detail}</span>}
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * 单只泳道展开：因果动作流（决策→挂单→追价→成交）为主叙事，其下嵌套订单树作逐笔取证。
 *
 * 逐只行默认折叠 → 多品种也只是多行摘要，钻进关心的那只才展开，天然 scale。
 */
function SymbolActionStream({
  lines,
  orders,
  currency,
  units,
}: {
  lines: ActionLine[]
  orders: ExecOrder[]
  currency: string
  units: DisplayUnits
}) {
  if (lines.length === 0) return <OrderTree orders={orders} currency={currency} units={units} />
  return (
    <details className="mt-1.5">
      <summary className="cursor-pointer select-none text-[12.5px] text-ink-3 hover:text-ink-2">
        动作流（{lines.length} 步）
      </summary>
      <div className="mt-1 border-l border-line pl-3">
        {lines.map((l) => (
          <div key={l.key} className="num flex items-baseline gap-2 py-0.5 text-[13px]">
            {l.time && <span className="w-16 flex-none text-ink-3">{l.time}</span>}
            {/* 三态：琥珀断点 / accent 蓝正向确认(成交·对账成功) / 中性过程步 */}
            <span className={l.broken ? 'font-medium text-warn' : l.good ? 'text-accent' : 'text-ink-2'}>{l.text}</span>
          </div>
        ))}
        <OrderTree orders={orders} currency={currency} units={units} />
      </div>
    </details>
  )
}

/**
 * 账户书签一行：`现金 A→B`（结构化，不摊 JSON）。
 *
 * ``pnl=true`` 时(仅权益)追加带号 Δ 并按红涨绿跌着色——权益差＝真盈亏，是本页唯一「色＝钱」处；
 * 现金/市值只是资金在现金与持仓间搬家、非盈亏，故保持中性，绝不误用涨跌色。
 */
function BookendRow({
  label,
  before,
  after,
  currency,
  pnl = false,
}: {
  label: string
  before: number | null
  after: number | null
  currency: string
  pnl?: boolean
}) {
  if (before == null && after == null) return null
  const showDelta = pnl && before != null && after != null && Math.abs(after - before) >= 0.005
  return (
    <span className="mr-4 inline-block">
      <span className="text-ink-3">{label} </span>
      {before != null ? withCurrency(fmtMoney(before), currency) : '—'}
      <span className="text-ink-3"> → </span>
      {after != null ? withCurrency(fmtMoney(after), currency) : '—'}
      {showDelta && before != null && after != null && (
        <span className={`font-semibold ${after === before ? 'text-ink-2' : after > before ? 'text-up' : 'text-down'}`}> ({fmtDelta(before, after)})</span>
      )}
    </span>
  )
}

/** ⑤ 证据：账户前后的结构化事实 + 一键复制原始附件（不再内联裸 JSON）。 */
function Evidence({
  m,
  currency,
  assetLabel,
  positionValueLabel,
  artifactsComplete,
}: {
  m: ExecutionDetailModel
  currency: string
  assetLabel: string
  positionValueLabel: string
  artifactsComplete: boolean
}) {
  const b = m.bookends
  const hasAccount = b.cashBefore != null || b.equityBefore != null || b.mvBefore != null
  return (
    <div className="mt-5">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold tracking-wide text-ink-3">证据</span>
        {m.artifacts.length > 0 && <ErrorCopyButton text={JSON.stringify(m.artifacts, null, 2)} label={`复制原始附件（${m.artifacts.length}）`} disabled={!artifactsComplete} />}

      </div>
      {hasAccount && (
        <div className="num text-[13.5px] text-ink-2">
          <BookendRow label="现金" before={b.cashBefore} after={b.cashAfter} currency={currency} />
          <BookendRow label={assetLabel} before={b.equityBefore} after={b.equityAfter} currency={currency} pnl />
          <BookendRow
            label={b.mvBeforeReference || b.mvAfterReference ? `${positionValueLabel} · 执行初始报价参考` : positionValueLabel}
            before={b.mvBefore}
            after={b.mvAfter}
            currency={currency}
          />
        </div>
      )}
      {b.timeoutSec != null && <div className="mt-1 text-[13px] text-ink-3">冻结输入 · 执行超时 {b.timeoutSec}s</div>}
    </div>
  )
}

/**
 * 执行详情路由页 /accounts/:id/executions/:executionId。
 *
 * 一台「可追溯的信任仪器」：把后端记录的这次执行信息都用上——头条结论、目标变化、逐只子链
 * （意图→决策→下单→成交→到位）、外层生命周期脊柱、可下钻的原始证据。成功时安静、失败时在
 * 断点处变红。
 */
export function ExecutionDetailPage() {
  const { id, executionId } = useParams()
  const location = useLocation()
  const journalPath = `/accounts/${Number(id)}/executions`
  const performancePath = `/accounts/${Number(id)}/history`
  const fromPerformance = (location.state as { performanceReturn?: string } | null)?.performanceReturn === performancePath
  const requestedReturn = (location.state as { journalReturn?: string } | null)?.journalReturn
  const journalReturn = requestedReturn === journalPath || requestedReturn?.startsWith(`${journalPath}?`) ? requestedReturn : journalPath
  const accountId = Number(id)
  const accounts = useDomainStore((s) => s.accounts)
  const item = accounts?.find((a) => a.account_id === accountId) ?? null
  const currency = currencyOf(item?.currency)
  const descriptor = useChannelDescriptor(item?.trade_channel)
  const units = quantityDisplayUnits(descriptor?.units ?? DEFAULT_UNITS)
  const positionValueLabel = positionValueLabelOf(descriptor?.ui)
  const assetTerms = accountAssetTerms(item?.trade_channel)

  const statusFetcher = useCallback(
    (signal: AbortSignal): Promise<ExecutionStatus> =>
      executionId ? getExecutionStatus(executionId, signal) : Promise.reject(new Error('缺少 execution_id')),
    [executionId],
  )
  const eventsFetcher = useCallback(
    (signal: AbortSignal): Promise<EvidencePage<ExecutionEvent>> =>
      executionId ? readEvidencePages((skip, limit) => getExecutionEvents(executionId, signal, { skip, limit }), signal) : Promise.resolve({ data: [], count: 0, complete: true, error: null }),
    [executionId],
  )
  const artifactsFetcher = useCallback(
    (signal: AbortSignal): Promise<EvidencePage<ExecutionArtifact>> =>
      executionId ? readEvidencePages((skip, limit) => getExecutionArtifacts(executionId, signal, { skip, limit }), signal) : Promise.resolve({ data: [], count: 0, complete: true, error: null }),
    [executionId],
  )
  // 本次执行是否仍在跑（服务端真源，SSE 秒级点亮）：在跑则短轮询让脊柱边跑边长，
  // 结束后回落到一次性取数（静态取证）。
  const running = useRunning(accountId)
  const isLive = running != null && running.executionId === executionId
  const pollMs = isLive ? 2500 : 0
  const status = usePolling(statusFetcher, {
    queryKey: `execution:${executionId ?? 'missing'}:status`,
    intervalMs: pollMs,
    enabled: Boolean(executionId),
  })
  const events = usePolling(eventsFetcher, {
    queryKey: `execution:${executionId ?? 'missing'}:events`,
    intervalMs: pollMs,
    enabled: Boolean(executionId),
  })
  const artifacts = usePolling(artifactsFetcher, {
    queryKey: `execution:${executionId ?? 'missing'}:artifacts`,
    intervalMs: pollMs,
    enabled: Boolean(executionId),
  })

  const loading = status.loading && events.loading && artifacts.loading
  const rawEvents = events.data?.data ?? []
  const hasAnyData = status.data != null || events.data != null || artifacts.data != null
  const model = hasAnyData ? buildExecutionDetail(rawEvents, artifacts.data?.data ?? [], status.data) : null

  return (
    <section>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          {/* 执行详情路径含动态段、无法纳入账户名 FLIP 门控（flip=false），结构仍与全域统一。 */}
          <AccountPageTitle
            accountId={accountId}
            page="执行详情"
            name={item?.name}
            channel={item?.trade_channel}
            market={item?.market}
            flip={false}
            back={{ to: fromPerformance ? performancePath : journalReturn, label: fromPerformance ? '返回实盘绩效' : '返回执行记录' }}
          />
        </div>
        {executionId && <EvidenceExportButton target={{ kind: 'execution', accountId, executionId }} accountName={item?.name}
          isRunning={isLive || ['QUEUED', 'RUNNING', 'TERMINATING'].includes(status.data?.status ?? '')}
          disabled={status.data != null && status.data.account_id !== accountId} />}
      </div>
      <div className="num mt-2 text-[16px] font-mono text-ink-2">{executionId}</div>
      <div>
        <ErrorNotice intent="uncertain" title="状态待确认：执行状态查询失败" error={status.error} contextKey={executionId} onRetry={status.refresh} />
      </div>
      <ExecutionErrorEvidence accountId={accountId} executionId={executionId} status={status.data} events={rawEvents} />
      {model && <Header m={model} currency={currency} assetLabel={assetTerms.shortLabel} />}

      {/* 运行中：顶部给一条确定态阶段条作一瞥总览；逐事件细节仍在下方脊柱。 */}
      {isLive && running && running.status === 'queued' && (
        <div className="mt-3 text-[15px] text-ink-2">已受理，等待开跑</div>
      )}
      {isLive && running && (running.status === 'running' || running.status === 'terminating') && (
        <div className="mt-3">
          <PhaseBar phase={running.phase} />
        </div>
      )}

      <Card className="mt-4 px-6 py-5">
        {loading && <SkeletonLines rows={5} />}
        <ErrorNotice intent="read" title="执行事件加载失败" error={events.error ?? events.data?.error} contextKey={executionId} onRetry={events.refresh} />
        <ErrorNotice intent="read" title="执行证据加载失败" error={artifacts.error ?? artifacts.data?.error} contextKey={executionId} onRetry={artifacts.refresh} />
        {model && (
          <>
            <TargetChangeView m={model} />
            {model.symbols.length > 0 ? (
              <>
                <div className="mb-2 text-xs font-semibold tracking-wide text-ink-3">逐只结果</div>
                {model.symbols.filter(s => model.conclusion.sessionScope === 'partial' || model.conclusion.state !== 'PARTIAL' || model.conclusion.symbols.length === 0 || model.conclusion.symbols.includes(s.symbol)).map((s) => {
                  const symbolUnits = quantityDisplayUnits(units)
                  return (
                    <SymbolChainRow
                      key={s.symbol}
                      s={s}
                      currency={currency}
                      units={symbolUnits}
                      lines={buildSymbolActionStream(rawEvents, s.symbol, {
                        quantityLabel: symbolUnits.quantity_label,
                        quantityMaxDecimals: symbolUnits.quantity_max_decimals,
                        priceLabel: symbolUnits.price_label || currency,
                      })}
                    />
                  )
                })}
              </>
            ) : (
              <p className="text-[14px] text-ink-3">
                {model.task?.status === 'FAILED' && model.task.started_at == null
                  ? '执行未进入执行链路，未产生逐只结果和账户快照。'
                  : '本次执行未产生逐只对账。'}
              </p>
            )}
            <Spine m={model} />
            <Evidence
              m={model}
              currency={currency}
              assetLabel={assetTerms.shortLabel}
              positionValueLabel={positionValueLabel}
              artifactsComplete={artifacts.data?.complete === true && !artifacts.error && !artifacts.loading && !artifacts.refreshing}
            />
          </>
        )}
      </Card>
    </section>
  )
}
