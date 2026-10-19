import { OverflowText } from '@/components/ui/OverflowText'
import { SizingEvidence } from '@/features/account/SizingEvidence'
import {
  executableTargetFallbackText,
  isQuantizedZero,
  quantityText,
  weightText,
} from '@/features/account/sizingEvidenceModel'
import { rebalancePlanOfServer, type RebalanceRow } from '@/lib/derive'
import { displayCurrencyUnit, fmtMoney, signedPct } from '@/lib/format'
import type { AccountRebalancePlan, LatestWeights, Position, TargetSizing } from '@/types/api'

const QTY_EPS = 1e-9

function pctLabel(pct: number): string {
  if (Math.abs(pct) < 0.05) return '空仓'
  return `${pct > 0 ? '多' : '空'}${Math.abs(pct).toFixed(1)}%`
}

function dirCls(pct: number): string {
  if (Math.abs(pct) < 0.05) return 'text-ink-1'
  return pct > 0 ? 'text-up' : 'text-down'
}

function axisX(value: number, scale: number): number {
  return scale > 0 ? 50 + (value / scale) * 44 : 50
}

/** 零锚持仓标尺：实心段配合斜纹调整段，翻向时分别表示目标侧与当前侧。 */
function PositionRuler({ row, scale }: { row: RebalanceRow; scale: number }) {
  const zero = axisX(0, scale)
  const current = axisX(row.cur, scale)
  const target = axisX(row.tgt, scale)
  const flip = row.action === 'flip'
  const aligned = row.action === 'aligned'
  const deepColor = flip ? 'bg-warn' : 'bg-accent'
  const lightColor = flip ? 'bg-warn-mid' : 'bg-accent-mid'

  let deep: { lo: number; hi: number }
  let light: { lo: number; hi: number }
  if (flip) {
    deep = { lo: Math.min(zero, target), hi: Math.max(zero, target) }
    light = { lo: Math.min(zero, current), hi: Math.max(zero, current) }
  } else {
    const near = Math.abs(row.cur) <= Math.abs(row.tgt) ? current : target
    const far = Math.abs(row.cur) <= Math.abs(row.tgt) ? target : current
    deep = { lo: Math.min(zero, near), hi: Math.max(zero, near) }
    light = { lo: Math.min(near, far), hi: Math.max(near, far) }
  }

  return (
    <div className="min-w-0">
      <div className="num mb-1.5 whitespace-nowrap text-center text-[12px] leading-none">
        <span className="text-ink-3">当前 </span>
        <span className={`font-semibold ${dirCls(row.cur)}`}>{pctLabel(row.cur)}</span>
        {!aligned && (
          <>
            <span className="text-ink-3"> → 目标 </span>
            <span className={`font-medium ${dirCls(row.tgt)}`}>{pctLabel(row.tgt)}</span>
          </>
        )}
      </div>
      <div className="relative h-4">
        <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-ink-1/10 contrast:bg-ink-3" />
        <div className="absolute inset-x-0 top-1/2 h-3 -translate-y-1/2 overflow-hidden rounded-sm bg-fill">
          {light.hi - light.lo > 0.1 && (
            <div
              className={`chart-hatch absolute inset-y-0 ${lightColor}`}
              style={{ left: `${light.lo}%`, width: `${light.hi - light.lo}%` }}
            />
          )}
          {deep.hi - deep.lo > 0.1 && (
            <div
              className={`absolute inset-y-0 ${deepColor}`}
              style={{ left: `${deep.lo}%`, width: `${deep.hi - deep.lo}%` }}
            />
          )}
        </div>
      </div>
    </div>
  )
}

function isShort(direction: unknown): boolean {
  return typeof direction === 'string' && (direction.includes('空') || direction.toLowerCase().includes('short'))
}

function signedPositionQuantity(position: Position): number | null {
  const extra = position.extra
  if (extra && typeof extra === 'object' && 'net_position' in extra) {
    const net = Number((extra as { net_position?: unknown }).net_position)
    if (Number.isFinite(net)) return net
  }
  const volume = Number(position.volume)
  if (!Number.isFinite(volume)) return null
  return isShort(position.direction) ? -Math.abs(volume) : Math.abs(volume)
}

function currentQuantities(positions: Position[]): Map<string, number> {
  const result = new Map<string, number>()
  for (const position of positions) {
    if (typeof position.symbol !== 'string') continue
    const quantity = signedPositionQuantity(position)
    if (quantity == null) continue
    result.set(position.symbol, (result.get(position.symbol) ?? 0) + quantity)
  }
  return result
}

/** 安静的逐只目标链：策略/账户/可执行/实际四层事实按需展开。 */
export function HoldingsView({
  positions,
  executablePlan,
  target,
  equity,
  currency = '',
  assetLabel,
  sizing = null,
  quantityLabel = '',
}: {
  positions: Position[]
  executablePlan: AccountRebalancePlan | null
  target: LatestWeights
  equity: number
  currency?: string
  assetLabel: string
  sizing?: TargetSizing | null
  quantityLabel?: string
}) {
  const plan = rebalancePlanOfServer(executablePlan)
  const scale = plan.rows.reduce((max, row) => Math.max(max, Math.abs(row.cur), Math.abs(row.tgt)), 0)
  const actual = currentQuantities(positions)
  const available = sizing?.status === 'available'
  const quantizedAligned = plan.rows.filter((row) => {
    const evidence = sizing?.rows[row.symbol]
    return row.action === 'aligned' && isQuantizedZero(evidence)
  }).length
  const ordinaryAligned = plan.rows.length - plan.off - quantizedAligned
  const heldCount = positions.filter((position) => Math.abs(signedPositionQuantity(position) ?? 0) > QTY_EPS).length

  if (plan.rows.length === 0) {
    return <p className="text-[14px] text-ink-2">当前空仓，且无目标持仓。</p>
  }

  return (
    <>
      <div className="text-[14px] text-ink-2">
        {plan.rows.length} 只
        {ordinaryAligned > 0 && <span> · {ordinaryAligned} 到位</span>}
        {quantizedAligned > 0 && <span className="text-ink-3"> · {quantizedAligned} 量化为0</span>}
      </div>
      <div className="num mt-1 mb-3 text-[12.5px] text-ink-3">
        当前 {heldCount === 0 ? '空仓' : `持有 ${heldCount} 只`}
        {equity > 0 && (
          <>
            {' · '}{assetLabel} {fmtMoney(equity)}
            {currency && ` ${displayCurrencyUnit(currency)}`}
          </>
        )}
        {' · '}净敞口 {signedPct(plan.netExposure)} → 目标 {signedPct(plan.targetNet)}
      </div>

      <div className="hidden grid-cols-[minmax(88px,0.55fr)_minmax(220px,1.2fr)_minmax(240px,1.45fr)_minmax(140px,0.8fr)] gap-4 py-1.5 text-[12px] text-ink-3 md:grid">
        <span>代码</span>
        <span className="text-center">空 ◄ 0 ► 多</span>
        <span>账户目标 → 可执行目标</span>
        <span className="text-right">实际持仓</span>
      </div>

      {plan.rows.map((row) => {
        const evidence = sizing?.rows[row.symbol]
        const current = actual.get(row.symbol) ?? 0
        const aligned = row.action === 'aligned'
        const quantized = isQuantizedZero(evidence)
        const actualText = quantityText(current, quantityLabel)
        const state = aligned ? (quantized ? '无需下单' : '到位') : '待调整'

        return (
          <div
            key={row.symbol}
            className="grid grid-cols-[minmax(82px,0.55fr)_minmax(0,1.8fr)] gap-x-3 border-t border-line py-3 md:grid-cols-[minmax(88px,0.55fr)_minmax(220px,1.2fr)_minmax(240px,1.45fr)_minmax(140px,0.8fr)] md:gap-x-4"
          >
            <div className="min-w-0 self-center">
              <OverflowText className="text-[14px] font-medium" text={row.symbol} />
            </div>
            <div className="min-w-0 self-center">
              <PositionRuler row={row} scale={scale} />
            </div>
            <div className="col-start-2 min-w-0 md:col-start-auto">
              {available && evidence ? (
                <SizingEvidence row={evidence} quantityLabel={quantityLabel} currency={currency} />
              ) : (
                <div className="num min-h-8 py-1.5 text-[13.5px] text-ink-2">
                  {weightText(target[row.symbol] ?? 0)}
                  <span className="text-ink-3">
                    {' → '}
                    {executableTargetFallbackText(sizing?.status, evidence, quantityLabel)}
                  </span>
                </div>
              )}
            </div>
            <div className="col-start-2 flex min-w-0 items-baseline justify-between gap-3 text-[12.5px] md:col-start-auto md:block md:self-center md:text-right">
              <span className="num text-ink-2">实际 {actualText}</span>
              <span className={aligned ? 'text-ink-3' : 'font-medium text-warn'}> · {state}</span>
            </div>
          </div>
        )
      })}
    </>
  )
}
