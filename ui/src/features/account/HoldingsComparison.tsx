import { useRef, useState, type ComponentProps, type CSSProperties, type ReactNode } from 'react'
import { ResizeHandle } from '@/components/ui/ResizeHandle'
import { Skeleton, SkeletonGroup } from '@/components/ui/Skeleton'
import { HoldingsPreviewTable } from '@/features/account/HoldingsPreviewTable'

const DEFAULT_SPLIT = 40
const MIN_SPLIT = 25
const MAX_SPLIT = 50

type PreviewProps = Omit<ComponentProps<typeof HoldingsPreviewTable>, 'columnWidths' | 'onColumnWidthsChange'>

export function HoldingsComparison({ observation, actions, summary, table }: {
  observation?: ReactNode
  actions?: ReactNode
  summary?: ReactNode
  table?: PreviewProps
}) {
  const comparison = useRef<HTMLDivElement>(null)
  const [split, setSplit] = useState(DEFAULT_SPLIT)
  const [columnWidths, setColumnWidths] = useState<number[] | null>(null)

  return <>
    <div className="mb-3 flex min-h-8 min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
      <div className="mr-auto flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="flex-none text-[14px] font-semibold text-ink-2">持仓 vs 目标</h3>
        {observation}
      </div>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
        {!!table?.holdings?.length && columnWidths && <button type="button"
          className="inline-flex min-h-6 flex-none cursor-pointer items-center rounded-md px-1.5 text-xs whitespace-nowrap text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          onClick={event => {
            setColumnWidths(null)
            if (event.detail === 0) comparison.current?.querySelector<HTMLElement>('table [role="separator"]')?.focus({ preventScroll: true })
          }}>
          重置列宽
        </button>}
        {actions}
      </div>
    </div>
    {table?.holdings === null ? (
      <SkeletonGroup label="正在加载持仓与目标对照" className="grid min-h-48 gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.618fr)]">
        <div className="space-y-3 border-t border-line pt-3">
          <Skeleton className="h-7 w-32" />
          <Skeleton className="h-4 w-44" />
          <Skeleton className="h-2 w-full" />
        </div>
        <div className="min-w-0 border-t border-line lg:border-t-0 lg:border-l lg:pl-5">
          <HoldingsPreviewTable {...table} />
        </div>
      </SkeletonGroup>
    ) : (summary || table) && (
      <div ref={comparison} className={`relative min-h-48 ${table ? 'grid gap-5 lg:grid-cols-[minmax(0,var(--holdings-split))_minmax(0,1fr)] lg:gap-0' : ''}`}
        style={{ '--holdings-split': `${split}%` } as CSSProperties}>
        <div className={`min-w-0 border-t border-line pt-3 ${table ? 'lg:mr-5' : ''}`}>{summary}</div>
        {table && <>
          <div className="min-w-0 border-t border-line lg:border-t-0 lg:pl-5">
            <HoldingsPreviewTable {...table} columnWidths={columnWidths} onColumnWidthsChange={setColumnWidths} />
          </div>
          <ResizeHandle aria-label="调整持仓摘要与表格宽度" orientation="vertical"
            value={split} min={MIN_SPLIT} max={MAX_SPLIT} step={2}
            aria-valuetext={`${split}%`} title="左右拖动调整宽度 · 双击恢复默认"
            className="absolute inset-y-0 z-10 hidden w-[7px] -translate-x-1/2 lg:block"
            style={{ left: 'var(--holdings-split)' }}
            valueAt={x => {
              const bounds = comparison.current!.getBoundingClientRect()
              const percent = Math.round((x - bounds.left) / bounds.width * 100)
              return Math.min(MAX_SPLIT, Math.max(MIN_SPLIT, percent))
            }}
            onChange={setSplit} onCommit={setSplit} onReset={() => setSplit(DEFAULT_SPLIT)} />
        </>}
      </div>
    )}
  </>
}
