/** 每日盈亏增量柱（纯 SVG）：把权益曲线的「累计视角」换成「每日增量视角」。 */

import { useId, type PointerEvent } from 'react'
import type { DailyBar } from '@/features/history/derive'

/** 相邻活跃日日历间隔超过该天数即在柱间插入「⋯」数据真空断点。 */
const VOID_GAP_DAYS = 3

/** 柱色：交易盈亏走红涨绿跌；资金搬家走中性（不算盈亏）。 */
function barColor(b: DailyBar): string {
  if (b.kind === 'transfer' || !Number.isFinite(b.delta) || b.delta === 0) return 'var(--color-ink-3)'
  return b.delta >= 0 ? 'var(--color-up)' : 'var(--color-down)'
}

/**
 * 每日增量柱。
 *
 * 基线 0 居中、Y 轴对称；X 轴按活跃日均匀排布（压掉数据真空），相邻活跃日
 * 间隔过大处插「⋯」断点竖标，诚实标出时间断裂。死时间是无柱而非假平线。
 */
export function DailyBars({
  bars,
  hoverIndex = null,
  onHover,
}: {
  bars: DailyBar[]
  /** 当前 hover 命中的柱下标（受控，由父级持有以驱动 hero 读数）。 */
  hoverIndex?: number | null
  /** 指针移到某柱槽位时回报其下标；离开时回报 null。 */
  onHover?: (index: number | null) => void
}) {
  const patternId = useId()
  if (bars.length === 0) {
    return <div className="py-8 text-center text-[14px] text-ink-3">数据点不足，无法绘制。</div>
  }
  const W = 760
  const H = 210
  const padL = 8
  const padR = 8
  const padT = 26
  const padB = 22
  const plotH = H - padT - padB
  const zeroY = padT + plotH / 2
  const maxAbs = Math.max(...bars.map((b) => Math.abs(b.delta)), 1)
  const n = bars.length
  const slotW = (W - padL - padR) / n
  const barW = Math.min(slotW * 0.5, 40)
  const cx = (i: number) => padL + slotW * (i + 0.5)
  const barLen = (delta: number) => (Math.abs(delta) / maxAbs) * (plotH / 2 - 4)

  const sgn = (v: number) => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(0)
  // 疏密自适应：柱太密则跳标日签，避免刷屏。
  const labelStep = Math.ceil(n / 16)

  /** 指针 x → 所在柱槽位下标（按 SVG 缩放折算回 viewBox 用户坐标）。 */
  const handleMove = (e: PointerEvent<SVGRectElement>) => {
    if (!onHover) return
    const svg = e.currentTarget.ownerSVGElement
    if (!svg) return
    const r = svg.getBoundingClientRect()
    const xu = ((e.clientX - r.left) / r.width) * W
    const i = Math.floor((xu - padL) / slotW)
    onHover(Math.max(0, Math.min(n - 1, i)))
  }

  const hovered = hoverIndex != null && hoverIndex >= 0 && hoverIndex < n ? hoverIndex : null

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full">
      <defs>
        <pattern id={patternId} width="6" height="6" patternUnits="userSpaceOnUse">
          <path d="M-1 1L1-1M0 6L6 0M5 7L7 5" stroke="var(--color-surface)" strokeWidth="1.5" />
        </pattern>
      </defs>
      {/* 零基线 */}
      <line x1={padL} y1={zeroY} x2={W - padR} y2={zeroY} stroke="var(--color-line)" strokeWidth={1} />

      {bars.map((b, i) => {
        const len = barLen(b.delta)
        const up = b.delta >= 0
        const y = up ? zeroY - len : zeroY
        const color = barColor(b)
        const showLabel = i % labelStep === 0 || i === n - 1
        return (
          <g key={i} opacity={hovered == null || hovered === i ? 1 : 0.35} style={{ transition: 'opacity .12s' }}>
            {/* 数据真空断点：本柱距上一活跃日间隔过大，柱前插虚线竖标 + ⋯ */}
            {b.gapDaysBefore > VOID_GAP_DAYS && i > 0 && (
              <g>
                <line
                  x1={padL + slotW * i}
                  y1={padT}
                  x2={padL + slotW * i}
                  y2={H - padB}
                  stroke="var(--color-ink-3)"
                  strokeDasharray="3 3"
                  strokeWidth={1}
                />
                <text x={padL + slotW * i} y={padT - 14} textAnchor="middle" fontSize={11} fill="var(--color-ink-3)">
                  ⋯
                </text>
              </g>
            )}
            <rect x={cx(i) - barW / 2} y={y} width={barW} height={Math.max(len, 0.8)} rx={2} fill={color} />
            {b.kind !== 'transfer' && b.delta < 0 && <rect className="market-down-pattern"
              x={cx(i) - barW / 2} y={y} width={barW} height={Math.max(len, 0.8)} rx={2}
              fill={`url(#${patternId})`} />}
            {/* 增量数值（贴柱端） */}
            <text
              x={cx(i)}
              y={up ? y - 4 : y + len + 11}
              textAnchor="middle"
              fontSize={10}
              className="num"
              fill={color}
            >
              {sgn(b.delta)}
            </text>
            {/* 日标签 */}
            {showLabel && (
              <text x={cx(i)} y={H - 7} textAnchor="middle" fontSize={10} fill="var(--color-ink-3)">
                {b.day}
              </text>
            )}
          </g>
        )
      })}

      {/* 透明捕获层（置顶）：接管指针，按槽位命中某柱，读数上抬到 hero。 */}
      {onHover && (
        <rect
          x={0}
          y={0}
          width={W}
          height={H}
          fill="transparent"
          style={{ cursor: 'pointer' }}
          onPointerMove={handleMove}
          onPointerLeave={() => onHover(null)}
        />
      )}
    </svg>
  )
}
