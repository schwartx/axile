import { OverflowText } from '@/components/ui/OverflowText'
import { ResizeHandle } from '@/components/ui/ResizeHandle'
import { Skeleton } from '@/components/ui/Skeleton'
import { formatHoldingQuantity } from '@/features/account/holdingPreview'
import type { HoldingPreviewRow } from '@/features/account/holdingDetailModel'
import { quantityText, targetQuantityText } from '@/features/account/sizingEvidenceModel'
import { fmtMoney, withCurrency } from '@/lib/format'
import { useLayoutEffect, useRef, useState } from 'react'

/**
 * 账户详情「持仓 vs 目标」右栏的当前持仓快照。
 *
 * 默认按内容排版，品种列吸收剩余空间。拖动时相邻两列互相让宽。
 */
const PREVIEW_TH = 'sticky top-0 z-[1] h-8 bg-surface align-middle font-medium'
const PREVIEW_NUMBER = 'whitespace-nowrap text-right'

const INITIAL_COLUMN = { width: 0, min: 0, fit: 0 }

function measureColumns(table: HTMLTableElement, header: HTMLTableRowElement, numberPadding: number) {
  const range = document.createRange()
  const textWidth = (element: Element) => {
    range.selectNodeContents(element)
    return range.getBoundingClientRect().width
  }
  const labelWidths = Array.from(header.cells, cell => textWidth(cell.firstElementChild!))
  const contentWidths = [...labelWidths]
  for (const row of table.tBodies[0].rows) {
    for (const [index, cell] of Array.from(row.cells).entries()) {
      const text = index === 0 ? cell.querySelector('.overflow-text-static')! : cell.firstElementChild!
      contentWidths[index] = Math.max(contentWidths[index], textWidth(text))
    }
  }
  return Array.from(header.cells, (cell, index) => ({
    width: cell.getBoundingClientRect().width,
    min: Math.ceil(index === 0 ? labelWidths[index] + numberPadding : contentWidths[index] + numberPadding),
    fit: Math.ceil(contentWidths[index] + (index === 0 ? 0 : numberPadding * 2)),
  }))
}

export function HoldingsPreviewTable({
  observed = false,
  holdings,
  currency = '',
  positionValueLabel,
  quantityMode = false,
  quantityLabel = '',
  compact = false,
  onSelect,
  columnWidths = null,
  onColumnWidthsChange,
}: {
  observed?: boolean
  /** `null` = 对照仍在加载，表结构在、单元格骨架占位。 */
  holdings: HoldingPreviewRow[] | null
  currency?: string
  positionValueLabel?: string
  quantityMode?: boolean
  quantityLabel?: string
  compact?: boolean
  onSelect?: (symbol: string, keyboardOpened: boolean) => void
  columnWidths?: number[] | null
  onColumnWidthsChange?: (widths: number[]) => void
}) {
  const viewport = useRef<HTMLDivElement>(null)
  const table = useRef<HTMLTableElement>(null)
  const headerRow = useRef<HTMLTableRowElement>(null)
  const dragStart = useRef<{ x: number; width: number } | null>(null)
  const [guide, setGuide] = useState<{ left: number; top: number; height: number } | null>(null)
  const [columns, setColumns] = useState<(typeof INITIAL_COLUMN)[]>([])
  const canResize = !!holdings?.length && !!onColumnWidthsChange
  const widths = canResize ? columnWidths : null
  const headers = ['品种', quantityMode ? '持仓/可用' : '仓位', quantityMode ? '目标数量' : '持仓/可用']
  if (positionValueLabel) headers.push(positionValueLabel)
  const numberClass = `${PREVIEW_NUMBER} ${widths ? 'pl-2' : 'pl-4'}`
  const numberTextClass = widths ? 'block truncate' : undefined

  useLayoutEffect(() => {
    const element = table.current
    const row = headerRow.current
    if (!canResize || !element || !row) return

    const padding = parseFloat(getComputedStyle(row.cells[1]).paddingLeft) / (widths ? 1 : 2)
    const measure = () => {
      const next = measureColumns(element, row, padding)
      setColumns(current => next.every(({ width, min, fit }, index) =>
        current[index]?.width === width && current[index].min === min && current[index].fit === fit)
        ? current : next)
    }
    const observer = new ResizeObserver(measure)
    measure()
    for (const cell of row.cells) observer.observe(cell, { box: 'border-box' })
    return () => observer.disconnect()
  }, [canResize, holdings, currency, quantityMode, quantityLabel, positionValueLabel, widths])

  const moveBoundary = (index: number, width: number) => {
    if (width === columns[index].width) return
    const next = columns.map(column => column.width)
    next[index + 1] += next[index] - width
    next[index] = width
    onColumnWidthsChange?.(next)
  }

  const previewBoundary = (index: number, width: number) => {
    const scrollport = viewport.current!
    const cell = headerRow.current!.cells[index]
    setGuide({
      left: cell.getBoundingClientRect().left - scrollport.getBoundingClientRect().left + scrollport.scrollLeft + width,
      top: scrollport.scrollTop,
      height: scrollport.clientHeight,
    })
  }

  return (
    <div
      ref={viewport}
      tabIndex={0}
      className={`quiet-scrollbar relative ${compact ? 'max-h-48' : 'h-48'} min-w-0 overflow-y-auto overscroll-contain pr-2 [scrollbar-gutter:stable] focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-2 focus-visible:outline-line`}
    >
      <table ref={table} aria-label={quantityMode ? '目标数量与持仓' : '当前持仓'}
        className={`${widths ? 'table-fixed' : 'w-full table-auto'} border-separate border-spacing-0`}
        style={widths ? { width: widths.reduce((total, width) => total + width, 0) } : undefined}>
        {widths && <colgroup>{widths.map((width, index) => <col key={index} style={{ width }} />)}</colgroup>}
        <thead>
          <tr ref={headerRow} className="text-[12px] font-medium text-ink-3">
            {headers.map((label, index) => {
              const resizeIndex = index - 1
              const resizeLabel = headers[resizeIndex]
              const column = columns[resizeIndex] ?? INITIAL_COLUMN
              const neighbor = columns[index] ?? INITIAL_COLUMN
              const min = Math.min(column.width, column.min)
              const max = column.width + Math.max(0, neighbor.width - neighbor.min)
              const clamp = (width: number) => Math.min(max, Math.max(min, width))
              const textLayout = index === 0
                ? `text-left whitespace-nowrap ${widths ? '' : 'w-full'}` : numberClass
              return <th key={index} scope="col" className={`${PREVIEW_TH} ${textLayout} border-b border-line`}>
                <span className={numberTextClass}>{label}</span>
                {canResize && index > 0 && <ResizeHandle
                  aria-label={`调整${resizeLabel}列宽`} orientation="vertical" showGrip={false}
                  value={column.width} min={min} max={max} step={8}
                  aria-valuetext={`${Math.round(column.width)} 像素`}
                  title={`拖动调整相邻两列宽度 · 双击贴合${resizeLabel}`}
                  className="absolute inset-y-2 left-1 w-1"
                  valueAt={x => {
                    const start = dragStart.current ??= { x, width: column.width }
                    return clamp(start.width + x - start.x)
                  }}
                  onResizeChange={dragging => {
                    dragStart.current = null
                    if (!dragging) setGuide(null)
                  }}
                  onChange={next => previewBoundary(resizeIndex, next)}
                  onCommit={next => moveBoundary(resizeIndex, next)}
                  onReset={() => moveBoundary(resizeIndex, clamp(column.fit))}
                />}
              </th>
            })}
          </tr>
        </thead>
        <tbody>
          {holdings == null ? (
            Array.from({ length: 5 }, (_, index) => (
              <tr key={index} className="border-t border-line first:border-t-0">
                <td className="h-8 w-full max-w-0">
                  <Skeleton className="h-3 w-12" />
                </td>
                <td className={`h-8 ${numberClass}`}>
                  <Skeleton className="ml-auto h-3 w-9" />
                </td>
                <td className={`h-8 ${numberClass}`}>
                  <Skeleton className="ml-auto h-3 w-12" />
                </td>
                {positionValueLabel && <td className={`h-8 ${numberClass}`}>
                  <Skeleton className="ml-auto h-3 w-14" />
                </td>}
              </tr>
            ))
          ) : holdings.length === 0 ? (
            <tr>
              <td colSpan={headers.length} className="h-40 text-[14px] text-ink-3">
                {observed ? '当前空仓' : '持仓尚未查询'}
              </td>
            </tr>
          ) : (
            holdings.map((holding) => {
              const dir = holding.direction === 'short' ? '空' : '多'
              const weight = holding.targetOnly ? (observed ? '空仓' : '未查询')
                : holding.weight == null ? `${dir}—` : `${dir}${Math.abs(holding.weight).toFixed(1)}%`
              const signedVolume = holding.direction === 'short' && holding.volume != null ? -holding.volume : holding.volume
              const volume = quantityMode ? quantityText(signedVolume) : formatHoldingQuantity(holding.volume)
              const availableVolume = quantityMode ? targetQuantityText(holding.availableVolume) : formatHoldingQuantity(holding.availableVolume)
              const needsAdjustment = quantityMode && holding.quantityTarget?.aligned === false
              const quantities = `${volume}/${availableVolume}`
              const quantitiesCell = <td className={`num h-8 ${numberClass} text-ink-2`}>
                <span className={numberTextClass} title={widths ? quantities : undefined}>{quantities}</span>
              </td>
              const value = holding.value == null ? '—' : withCurrency(fmtMoney(holding.value), currency)
              const targetValue = quantityMode ? quantityText(holding.quantityTarget?.target_quantity ?? holding.snapshotQuantity, quantityLabel) : weight
              return (
                <tr key={holding.key} onClick={onSelect ? () => onSelect(holding.symbol, false) : undefined}
                  className={`border-t border-line text-[13.5px] first:border-t-0 ${onSelect ? 'cursor-pointer hover:bg-fill has-[:focus-visible]:bg-fill' : ''}`}>
                  <td className={`h-8 ${widths ? '' : 'w-full max-w-0'} text-[14px] font-[560] text-ink-1`}>{onSelect ? <button type="button" aria-haspopup="dialog" aria-label={`查看 ${holding.symbol} 持仓与目标`}
                    onClick={event => { event.stopPropagation(); onSelect(holding.symbol, event.detail === 0) }}
                    className="block min-h-8 w-full cursor-pointer text-left focus-visible:rounded-sm focus-visible:outline-2 focus-visible:outline-accent">
                    <OverflowText text={holding.symbol} className="block" />
                  </button> : <OverflowText text={holding.symbol} className="block" />}</td>
                  {quantityMode && quantitiesCell}
                  <td className={`num h-8 ${numberClass} ${needsAdjustment ? 'font-semibold text-warn' : 'text-ink-2'}`}>
                    <span className={numberTextClass} title={widths ? targetValue : undefined}>{targetValue}</span>
                    {needsAdjustment && <span className="sr-only"> · 待调整</span>}
                  </td>
                  {!quantityMode && quantitiesCell}
                  {positionValueLabel && <td className={`num h-8 ${numberClass} font-medium text-ink-1`}>
                    <span className={numberTextClass} title={widths ? value : undefined}>{value}</span>
                  </td>}
                </tr>
              )
            })
          )}
        </tbody>
      </table>
      {guide && <div aria-hidden="true" className="pointer-events-none absolute z-[2] w-px bg-accent" style={guide} />}
    </div>
  )
}

