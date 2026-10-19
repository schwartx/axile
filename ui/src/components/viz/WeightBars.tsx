/**
 * 品种权重条形列表（纯 DOM，无图表库）。
 *
 * 每行 = 品种 + 行内条 + 数值。条长按 |w| / max|w| 归一，满宽者为最大权重；
 * 大小关系走条长（前注意加工），数字只承载精确值。
 *
 * 颜色纪律：权重是持仓配置而非盈亏/偏离，全中性 ink；多空以明度区分
 * （空头更浅），不碰红绿色相，负号由数值本身表达。
 *
 * 动效纪律：条宽 200ms 布局流过渡（同一品种的条还是那条条），首帧就位，
 * 不入场表演；reduce 偏好下关闭。
 */
import { OverflowText } from '@/components/ui/OverflowText'

interface WeightBarsProps {
  /** 品种 → 权重。组件内统一过滤零权重并按 |w| 降序，调用方无需预处理。 */
  weights: Record<string, number> | Array<readonly [string, number]>
  /** 数值格式：`percent` 为百分数一位小数（默认）；`raw` 为原始小数四位（函数调试场景）。 */
  format?: 'percent' | 'raw'
  className?: string
}

export function WeightBars({ weights, format = 'percent', className = '' }: WeightBarsProps) {
  const rows = (Array.isArray(weights) ? weights.slice() : Object.entries(weights))
    .filter(([, w]) => Math.abs(w) > 1e-9)
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
  if (rows.length === 0) return null
  const max = Math.abs(rows[0][1])
  return (
    <div className={className} role="list" aria-label="品种权重">
      {rows.map(([sym, w]) => (
        <div key={sym} role="listitem" className="flex items-center gap-3 py-1.5 text-[14.5px]">
          <OverflowText className="w-20 flex-none font-[520] text-ink-1" text={sym} />
          <div aria-hidden className="h-1.5 min-w-0 flex-1">
            <div
              className={`h-full rounded-full transition-[width] duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${
                w < 0 ? 'market-negative-weight bg-ink-3/45 contrast:bg-ink-3' : 'bg-ink-2/65 contrast:bg-ink-2'
              }`}
              style={{ width: `${((Math.abs(w) / max) * 100).toFixed(2)}%` }}
            />
          </div>
          <span className="num w-16 flex-none text-right font-medium text-ink-1">
            {format === 'raw' ? w.toFixed(4) : `${(w * 100).toFixed(1)}%`}
          </span>
        </div>
      ))}
    </div>
  )
}
