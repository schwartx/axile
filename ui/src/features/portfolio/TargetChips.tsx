import { useLayoutEffect, useRef, useState } from 'react'
import { Chip } from '@/components/ui/Card'
import {
  formatTargetWeight,
  targetDirectionClass,
  type PortfolioTargetEntry,
} from './portfolioCardSummary'

/** chip 行间距，与 Tailwind 的 gap-1.5 保持一致。 */
const CHIP_GAP = 6
/** chip 行最多占用的行数；超过才折叠进「另有 N 个」。 */
const MAX_ROWS = 3

/**
 * 组合卡片的品种 chip 行：按容器实际宽度最多铺 3 行，摆不下的折叠进「另有 N 个」。
 * compact 用于向导，单行展示、无分隔线，溢出数量显示为 +N。
 * 可见数量是布局决策，不写死在数据层；测量在 useLayoutEffect 内完成，首帧即收敛，无入场跳变。
 */
export function TargetChips({ entries, compact = false }: { entries: PortfolioTargetEntry[]; compact?: boolean }) {
  const maxRows = compact ? 1 : MAX_ROWS
  const moreLabel = (count: number) => compact ? `+${count}` : `另有 ${count} 个`
  const rowRef = useRef<HTMLDivElement>(null)
  const measureRef = useRef<HTMLDivElement>(null)
  const [visibleCount, setVisibleCount] = useState(entries.length)
  // 权重刷新会改 chip 文案宽度，内容签名驱动重测；条目数只影响签名，不单独依赖。
  const signature = entries.map((entry) => `${entry.symbol}:${entry.weight}`).join('|')

  useLayoutEffect(() => {
    const row = rowRef.current
    const measure = measureRef.current
    if (!row || !measure) return
    const measured = Array.from(measure.children) as HTMLElement[]
    if (measured.length < 2) return
    // 末位是按「只剩 1 个可见」预留的最宽折叠占位，实际渲染的「另有 N 个」只会更窄。
    const widths = measured.slice(0, -1).map((element) => element.offsetWidth)
    const reserve = measured[measured.length - 1].offsetWidth
    const total = widths.length

    // 贪心模拟 flex-wrap：逐行从左往右摆，最后一行按需给折叠占位预留宽度。
    const greedy = (reserveLastRow: boolean): number => {
      const availableWidth = row.clientWidth
      const capacityOf = (rowIndex: number): number =>
        reserveLastRow && rowIndex === maxRows - 1 ? availableWidth - reserve - CHIP_GAP : availableWidth
      let used = 0
      let rowIndex = 0
      let count = 0
      for (let index = 0; index < total; index += 1) {
        const width = widths[index]
        const next = used === 0 ? width : used + CHIP_GAP + width
        if (next <= capacityOf(rowIndex)) {
          used = next
          count = index + 1
          continue
        }
        if (rowIndex + 1 >= maxRows) return count
        rowIndex += 1
        used = 0
        if (width <= capacityOf(rowIndex)) {
          used = width
          count = index + 1
        } else {
          return count
        }
      }
      return count
    }

    const fit = () => {
      const all = greedy(false)
      // 全部能摆下就不留折叠占位；否则在最后一行预留宽度再算一遍，保证「另有 N 个」不溢到第 4 行。
      setVisibleCount(all >= total ? total : greedy(true))
    }

    fit()
    const observer = new ResizeObserver(fit)
    observer.observe(row)
    return () => observer.disconnect()
  }, [signature, maxRows])

  const hiddenCount = entries.length - visibleCount
  return (
    <div ref={rowRef} className={compact ? "relative flex h-6 min-w-0 flex-1 items-center gap-1.5 overflow-hidden" : "relative mt-3 flex flex-wrap items-center gap-1.5 border-t border-line pt-3"}>
      {/* 测量层：保持布局但不显示，读到的即各 chip 的固有宽度。 */}
      <div ref={measureRef} className="invisible absolute left-0 top-0 flex items-center gap-1.5" aria-hidden="true">
        {entries.map((entry) => (
          <TargetChip key={entry.symbol} entry={entry} />
        ))}
        {(entries.length > 1 || compact) && <Chip className="flex-none whitespace-nowrap text-ink-3">{moreLabel(entries.length)}</Chip>}
      </div>
      {entries.slice(0, visibleCount).map((entry) => (
        <TargetChip key={entry.symbol} entry={entry} />
      ))}
      {hiddenCount > 0 && <Chip className="flex-none whitespace-nowrap text-ink-3">{moreLabel(hiddenCount)}</Chip>}
    </div>
  )
}

function TargetChip({ entry }: { entry: PortfolioTargetEntry }) {
  return (
    <Chip className="flex flex-none items-center gap-1.5">
      <span className="truncate" title={entry.symbol}>{entry.symbol}</span>
      <span className={`font-[550] tabular-nums ${targetDirectionClass(entry.weight)}`}>
        {formatTargetWeight(entry.weight)}
      </span>
    </Chip>
  )
}
