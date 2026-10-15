import { OverflowText } from '@/components/ui/OverflowText'
import { Skeleton } from '@/components/ui/Skeleton'
import { formatHoldingQuantity } from '@/features/account/holdingPreview'
import type { HoldingPreviewRow } from '@/features/account/holdingDetailModel'
import { fmtMoney, withCurrency } from '@/lib/format'

/**
 * 账户详情「持仓 vs 目标」右栏的当前持仓快照。
 *
 * 表头与行共用固定列宽：品种 22%、仓位 25%、持仓/可用 25%、货值 28%。
 * 品种左对齐、数字右对齐；滚动槽前保留间距，长内容在各自列内呈现。
 * 表头 sticky 钉在滚动容器顶。
 */
const PREVIEW_TH = 'sticky top-0 z-[1] h-8 bg-surface align-middle font-medium'
const PREVIEW_COL = 'whitespace-nowrap'

export function HoldingsPreviewTable({
  observed = false,
  holdings,
  currency = '',
  positionValueLabel,
  onSelect,
}: {
  observed?: boolean
  /** `null` = 对照仍在加载，表结构在、单元格骨架占位。 */
  holdings: HoldingPreviewRow[] | null
  currency?: string
  positionValueLabel: string
  onSelect?: (symbol: string) => void
}) {
  return (
    <div
      tabIndex={0}
      className="quiet-scrollbar h-48 min-w-0 overflow-y-auto overscroll-contain pr-2 [scrollbar-gutter:stable] focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-2 focus-visible:outline-line"
    >
      <table aria-label="当前持仓" className="w-full table-fixed border-separate border-spacing-0">
        <colgroup>
          <col className="w-[22%]" />
          <col className="w-[25%]" />
          <col className="w-[25%]" />
          <col className="w-[28%]" />
        </colgroup>
        <thead>
          <tr className="text-[12px] font-medium text-ink-3">
            <th scope="col" className={`${PREVIEW_TH} ${PREVIEW_COL} border-b border-line pr-1.5 text-left`}>
              品种
            </th>
            <th scope="col" className={`${PREVIEW_TH} ${PREVIEW_COL} border-b border-line px-1.5 text-right`}>
              仓位
            </th>
            <th scope="col" className={`${PREVIEW_TH} ${PREVIEW_COL} border-b border-line px-1.5 text-right`}>
              持仓/可用
            </th>
            <th scope="col" className={`${PREVIEW_TH} border-b border-line pl-1.5 text-right whitespace-nowrap`}>
              {positionValueLabel}
            </th>
          </tr>
        </thead>
        <tbody>
          {holdings == null ? (
            Array.from({ length: 5 }, (_, index) => (
              <tr key={index} className="border-t border-line first:border-t-0">
                <td className={`h-8 ${PREVIEW_COL} pr-1.5`}>
                  <Skeleton className="h-3 w-12" />
                </td>
                <td className={`h-8 ${PREVIEW_COL} px-1.5`}>
                  <Skeleton className="ml-auto h-3 w-9" />
                </td>
                <td className={`h-8 ${PREVIEW_COL} px-1.5`}>
                  <Skeleton className="ml-auto h-3 w-12" />
                </td>
                <td className="h-8 pl-1.5 whitespace-nowrap">
                  <Skeleton className="ml-auto h-3 w-14" />
                </td>
              </tr>
            ))
          ) : holdings.length === 0 ? (
            <tr>
              <td colSpan={4} className="h-40 text-[14px] text-ink-3">
                {observed ? '当前空仓' : '持仓尚未查询'}
              </td>
            </tr>
          ) : (
            holdings.map((holding) => {
              const dir = holding.direction === 'short' ? '空' : '多'
              const weight = holding.targetOnly ? (observed ? '空仓' : '未查询')
                : holding.weight == null ? `${dir}—` : `${dir}${Math.abs(holding.weight).toFixed(1)}%`
              const volume = formatHoldingQuantity(holding.volume)
              const availableVolume = formatHoldingQuantity(holding.availableVolume)
              const value = holding.value == null ? '—' : withCurrency(fmtMoney(holding.value), currency)
              return (
                <tr key={holding.key} onClick={onSelect ? event => {
                  event.currentTarget.querySelector('button')?.focus()
                  onSelect(holding.symbol)
                } : undefined}
                  className={`border-t border-line text-[13.5px] first:border-t-0 ${onSelect ? 'cursor-pointer hover:bg-fill focus-within:bg-fill' : ''}`}>
                  <td className={`h-8 ${PREVIEW_COL} pr-1.5 text-[14px] font-[560] text-ink-1`}>{onSelect ? <button type="button" aria-haspopup="dialog" aria-label={`查看 ${holding.symbol} 持仓与目标`}
                    onClick={event => { event.stopPropagation(); onSelect(holding.symbol) }}
                    className="block min-h-8 w-full cursor-pointer text-left focus-visible:rounded-sm focus-visible:outline-2 focus-visible:outline-accent">
                    <OverflowText text={holding.symbol} className="block" />
                  </button> : <OverflowText text={holding.symbol} className="block" />}</td>
                  <td className={`num h-8 ${PREVIEW_COL} px-1.5 text-right text-ink-2`}><OverflowText text={weight} className="block" /></td>
                  <td className={`num h-8 ${PREVIEW_COL} px-1.5 text-right text-ink-2`}>
                    <OverflowText text={`${volume}/${availableVolume}`} className="block" />
                  </td>
                  <td className="num h-8 pl-1.5 text-right font-medium whitespace-nowrap text-ink-1"><OverflowText text={value} className="block" /></td>
                </tr>
              )
            })
          )}
        </tbody>
      </table>
    </div>
  )
}

