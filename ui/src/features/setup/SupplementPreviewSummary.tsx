import NumberFlow from '@number-flow/react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { MOTION_LAYOUT } from '@/lib/viewTransition'

export interface SupplementPreviewValue {
  count: number
  configuredCount: number
}

const NUMBER_PROPS = {
  locales: 'zh-CN',
  format: { useGrouping: false },
  spinTiming: { duration: 220, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' },
  respectMotionPreference: true,
}

/** 正常轮次留白，仅提示被交易时段或下一轮基础触发裁剪的补发。 */
export function SupplementPreviewSummary({ value }: { value?: SupplementPreviewValue }) {
  if (!value) return null
  const { count, configuredCount } = value
  const clipped = count < configuredCount
  const text = clipped ? (count > 0 ? `仅补 ${count} 次` : '无补发') : ''
  return (
    <span aria-label={text || undefined} title={text || undefined} className="inline-flex min-w-0 items-baseline align-bottom">
      <span className="inline-flex max-w-full items-baseline">
        <span aria-hidden="true"><InkRewrite text={clipped ? (count > 0 ? '仅补' : '无补发') : ''} /></span>
        <span aria-hidden="true" className={`grid transition-[grid-template-columns] ${MOTION_LAYOUT} ${clipped && count > 0 ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]'}`}>
          <span className="min-w-0 overflow-hidden">
            <span className="inline-flex w-max items-baseline whitespace-pre">
              <NumberFlow {...NUMBER_PROPS} value={count} prefix=" " suffix=" 次" animated={clipped && count > 0} />
            </span>
          </span>
        </span>
      </span>
    </span>
  )
}
