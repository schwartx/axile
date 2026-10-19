/** 持仓敞口分布条（纯 SVG/DOM，无图表库）。 */
import { barColor } from '@/lib/derive'

/** 传入各持仓的市值（或权重）绝对值，渲染按比例分段的横条。 */
export function ExposureBar({ weights }: { weights: number[] }) {
  const ws = weights.map(Math.abs).filter((w) => w > 0)
  if (ws.length === 0) return null
  const total = ws.reduce((s, w) => s + w, 0) || 1
  return (
    <div className="mt-2 flex h-2 overflow-hidden rounded-full bg-line">
      {ws.map((w, i) => (
        <span
          key={i}
          className="exposure-segment block h-full [&+&]:border-l [&+&]:border-surface"
          style={{ width: `${((w / total) * 100).toFixed(2)}%`, background: `var(--exposure-segment-color, ${barColor(i)})` }}
        />
      ))}
    </div>
  )
}
