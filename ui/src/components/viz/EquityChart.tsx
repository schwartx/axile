/** 权益（估值）曲线：面积 + 折线，纯 SVG。支持竖向标注（换绑 / 疑似资金进出）、数据真空虚线与 hover 准星。 */

import type { PointerEvent } from 'react'

/** 相邻两点日历间隔超过该天数即视为「数据真空」，该段改虚线且不填充。 */
const VOID_GAP_DAYS = 7

export interface EquityPoint {
  /** 展示用短标签（MM-DD HH:mm）。 */
  date: string
  /** 原始 ISO 时间戳（用于真空判断与按日聚合）。 */
  iso: string
  eq: number
}

export interface ChartMarker {
  /** 对应 points 的下标。 */
  index: number
  label: string
  /** 线与文字色，默认灰。 */
  color?: string
}

/** 两个 ISO 时间戳间隔的日历天数。 */
function gapDays(aIso: string, bIso: string): number {
  const a = new Date(aIso).getTime()
  const b = new Date(bIso).getTime()
  if (!Number.isFinite(a) || !Number.isFinite(b)) return 0
  return Math.abs(b - a) / 864e5
}

/** 按数据真空把下标切成若干连续段（段内相邻间隔均 ≤ 阈值）。 */
function splitRuns(points: EquityPoint[]): number[][] {
  const runs: number[][] = []
  let cur: number[] = [0]
  for (let i = 1; i < points.length; i++) {
    if (gapDays(points[i - 1].iso, points[i].iso) > VOID_GAP_DAYS) {
      runs.push(cur)
      cur = [i]
    } else {
      cur.push(i)
    }
  }
  runs.push(cur)
  return runs
}

/**
 * 权益（估值）曲线。
 *
 * X 轴按点序均匀排布；相邻点跨越 `VOID_GAP_DAYS` 天以上的「数据真空」段
 * 以中性虚线连接且不填充，诚实表达「此处无数据、非持平」。
 */
export function EquityChart({
  points,
  markers = [],
  hoverIndex = null,
  onHover,
}: {
  points: EquityPoint[]
  markers?: ChartMarker[]
  /** 当前 hover 命中的点下标（受控，由父级持有以驱动 hero 读数）。 */
  hoverIndex?: number | null
  /** 指针沿图移动时回报最近点下标；离开时回报 null。 */
  onHover?: (index: number | null) => void
}) {
  if (points.length < 2) {
    return <div className="py-8 text-center text-[14px] text-ink-3">数据点不足，无法绘制曲线。</div>
  }
  const W = 760
  const H = 210
  const padL = 6
  const padR = 6
  const padT = 22
  const padB = 20
  const eqs = points.map((p) => p.eq)
  const min = Math.min(...eqs)
  const max = Math.max(...eqs)
  const rng = max - min || 1
  const n = points.length
  const X = (i: number) => padL + (i * (W - padL - padR)) / (n - 1)
  const Y = (v: number) => padT + (H - padT - padB) * (1 - (v - min) / rng)

  const runs = splitRuns(points)

  /** 指针 x → 最近点下标（把视口坐标按 SVG 缩放折算回 viewBox 用户坐标）。 */
  const handleMove = (e: PointerEvent<SVGRectElement>) => {
    if (!onHover) return
    const svg = e.currentTarget.ownerSVGElement
    if (!svg) return
    const r = svg.getBoundingClientRect()
    const xu = ((e.clientX - r.left) / r.width) * W
    const i = Math.round(((xu - padL) / (W - padL - padR)) * (n - 1))
    onHover(Math.max(0, Math.min(n - 1, i)))
  }

  const hovered = hoverIndex != null && hoverIndex >= 0 && hoverIndex < n ? hoverIndex : null

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full">
      {/* 各连续段：面积 + 实线；真空段以中性虚线连接、不填充。 */}
      {runs.map((run, ri) => {
        if (run.length < 2) return null
        const line = run.map((i) => `${X(i).toFixed(1)},${Y(points[i].eq).toFixed(1)}`).join(' ')
        const first = run[0]
        const last = run[run.length - 1]
        const area = `${X(first).toFixed(1)},${H - padB} ${line} ${X(last).toFixed(1)},${H - padB}`
        return (
          <g key={`run-${ri}`}>
            <polygon points={area} fill="var(--color-equity-area)" />
            <polyline points={line} fill="none" stroke="var(--color-accent)" strokeWidth={1.8} strokeLinejoin="round" />
          </g>
        )
      })}
      {runs.slice(1).map((run, ri) => {
        // 上一段末点 → 本段首点的真空连接线（虚线，中性）。
        const prevLast = runs[ri][runs[ri].length - 1]
        const curFirst = run[0]
        const xm = (X(prevLast) + X(curFirst)) / 2
        // 标签置于连接线中段（而非顶部），避开左上角的 Y 轴刻度标签。
        const ym = (Y(points[prevLast].eq) + Y(points[curFirst].eq)) / 2
        return (
          <g key={`void-${ri}`}>
            <polyline
              points={`${X(prevLast).toFixed(1)},${Y(points[prevLast].eq).toFixed(1)} ${X(curFirst).toFixed(1)},${Y(points[curFirst].eq).toFixed(1)}`}
              fill="none"
              stroke="var(--color-ink-3)"
              strokeWidth={1}
              strokeDasharray="4 4"
            />
            <text x={xm + 6} y={ym} textAnchor="start" fontSize={9} fill="var(--color-ink-3)">
              无数据
            </text>
          </g>
        )
      })}
      {markers.map((m, k) => {
        if (m.index <= 0 || m.index >= n) return null
        const x = X(m.index)
        const color = m.color ?? 'var(--color-ink-3)'
        return (
          <g key={k}>
            <line x1={x} y1={padT} x2={x} y2={H - padB} stroke={color} strokeDasharray="3 3" strokeWidth={1} />
            <text x={x + 4} y={14} fontSize={10} fill={color}>
              {m.label}
            </text>
          </g>
        )
      })}
      <text x={padL} y={13} fontSize={10} fill="var(--color-ink-3)">
        {max.toFixed(1)}
      </text>
      <text x={padL} y={H - padB + 2} fontSize={10} fill="var(--color-ink-3)">
        {min.toFixed(1)}
      </text>
      <text x={X(0)} y={H - 6} textAnchor="start" fontSize={10} fill="var(--color-ink-3)">
        {points[0].date}
      </text>
      <text x={X(n - 1)} y={H - 6} textAnchor="end" fontSize={10} fill="var(--color-ink-3)">
        {points[n - 1].date}
      </text>

      {/* hover 准星：竖引导线 + 吸附到真实点的高亮圆 + 脚下就近日期标签（锚住竖线，不让它悬空指向虚无）。 */}
      {hovered != null &&
        (() => {
          const cxp = X(hovered)
          const label = points[hovered].date
          const tagW = label.length * 5.6 + 10
          const half = tagW / 2
          const tagX = Math.max(padL + half, Math.min(W - padR - half, cxp))
          return (
            <g pointerEvents="none">
              <line x1={cxp} y1={padT} x2={cxp} y2={H - padB} stroke="var(--color-ink-3)" strokeWidth={1} />
              <circle
                cx={cxp}
                cy={Y(points[hovered].eq)}
                r={3.5}
                fill="var(--color-accent)"
                stroke="var(--color-surface)"
                strokeWidth={1.5}
              />
              {/* 脚下日期标签：surface 底遮住其后的静态轴标签，crisp 可读；就近定位胜过只在 hero 顶部。 */}
              <rect x={tagX - half} y={H - 15} width={tagW} height={13} rx={3} fill="var(--color-surface)" stroke="var(--color-line)" strokeWidth={0.5} />
              <text x={tagX} y={H - 5} textAnchor="middle" fontSize={10} className="num" fill="var(--color-ink-1)">
                {label}
              </text>
            </g>
          )
        })()}

      {/* 透明捕获层（置顶）：接管指针，沿图 scrub 反查最近点。 */}
      {onHover && (
        <rect
          x={0}
          y={0}
          width={W}
          height={H}
          fill="transparent"
          style={{ cursor: 'crosshair' }}
          onPointerMove={handleMove}
          onPointerLeave={() => onHover(null)}
        />
      )}
    </svg>
  )
}
