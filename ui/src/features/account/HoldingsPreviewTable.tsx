import { OverflowText } from '@/components/ui/OverflowText'
import { Skeleton } from '@/components/ui/Skeleton'
import { formatHoldingQuantity } from '@/features/account/holdingPreview'
import type { HoldingPreviewRow } from '@/features/account/holdingDetailModel'
import { fmtMoney, withCurrency } from '@/lib/format'

/**
 * 账户详情「持仓 vs 目标」右栏的当前持仓快照。
 *
 * 数字列由表头与内容共同决定宽度，列间统一留白；品种吸收剩余空间。
 * 品种左对齐、超长代码在列内截断，数字右对齐并完整显示。
 * 表头 sticky 钉在滚动容器顶。
 */
const PREVIEW_TH = 'sticky top-0 z-[1] h-8 bg-surface align-middle font-medium'
const PREVIEW_NUMBER = 'whitespace-nowrap pl-4 text-right'

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
  onSelect?: (symbol: string, keyboardOpened: boolean) => void
}) {
  return (
    <div
      tabIndex={0}
      className="quiet-scrollbar h-48 min-w-0 overflow-y-auto overscroll-contain pr-2 [scrollbar-gutter:stable] focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-2 focus-visible:outline-line"
    >
      <table aria-label="当前持仓" className="w-full table-auto border-separate border-spacing-0">
        <thead>
          <tr className="text-[12px] font-medium text-ink-3">
            <th scope="col" className={`${PREVIEW_TH} w-full border-b border-line text-left whitespace-nowrap`}>
              品种
            </th>
            <th scope="col" className={`${PREVIEW_TH} ${PREVIEW_NUMBER} border-b border-line`}>
              仓位
            </th>
            <th scope="col" className={`${PREVIEW_TH} ${PREVIEW_NUMBER} border-b border-line`}>
              持仓/可用
            </th>
            <th scope="col" className={`${PREVIEW_TH} ${PREVIEW_NUMBER} border-b border-line`}>
              {positionValueLabel}
            </th>
          </tr>
        </thead>
        <tbody>
          {holdings == null ? (
            Array.from({ length: 5 }, (_, index) => (
              <tr key={index} className="border-t border-line first:border-t-0">
                <td className="h-8 w-full max-w-0">
                  <Skeleton className="h-3 w-12" />
                </td>
                <td className={`h-8 ${PREVIEW_NUMBER}`}>
                  <Skeleton className="ml-auto h-3 w-9" />
                </td>
                <td className={`h-8 ${PREVIEW_NUMBER}`}>
                  <Skeleton className="ml-auto h-3 w-12" />
                </td>
                <td className={`h-8 ${PREVIEW_NUMBER}`}>
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
                <tr key={holding.key} onClick={onSelect ? () => onSelect(holding.symbol, false) : undefined}
                  className={`border-t border-line text-[13.5px] first:border-t-0 ${onSelect ? 'cursor-pointer hover:bg-fill has-[:focus-visible]:bg-fill' : ''}`}>
                  <td className="h-8 w-full max-w-0 text-[14px] font-[560] text-ink-1">{onSelect ? <button type="button" aria-haspopup="dialog" aria-label={`查看 ${holding.symbol} 持仓与目标`}
                    onClick={event => { event.stopPropagation(); onSelect(holding.symbol, event.detail === 0) }}
                    className="block min-h-8 w-full cursor-pointer text-left focus-visible:rounded-sm focus-visible:outline-2 focus-visible:outline-accent">
                    <OverflowText text={holding.symbol} className="block" />
                  </button> : <OverflowText text={holding.symbol} className="block" />}</td>
                  <td className={`num h-8 ${PREVIEW_NUMBER} text-ink-2`}>{weight}</td>
                  <td className={`num h-8 ${PREVIEW_NUMBER} text-ink-2`}>
                    {volume}/{availableVolume}
                  </td>
                  <td className={`num h-8 ${PREVIEW_NUMBER} font-medium text-ink-1`}>{value}</td>
                </tr>
              )
            })
          )}
        </tbody>
      </table>
    </div>
  )
}

