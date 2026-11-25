import { useState } from 'react'
import { useNavigate } from '@/components/ui/nav'
import type { ReactNode } from 'react'

const PAGE_GUTTERS = 'px-5 sm:px-12'
const PAGE_FRAME = `mx-auto w-full max-w-[1728px] ${PAGE_GUTTERS}`

/** 向导页头：kicker / 标题 / 引导语。可单独塞进两列布局的左列。 */
export function WizardHeading({
  kicker,
  title,
  lead,
}: {
  kicker?: string
  title: string
  lead?: string
}) {
  return (
    <>
      {kicker && <div className="mb-1.5 text-xs font-semibold tracking-wide text-accent">{kicker}</div>}
      <div className="text-[23px] font-[680] tracking-tight">{title}</div>
      {lead ? <div className="mt-1.5 max-w-[560px] text-[15px] text-ink-2">{lead}</div> : null}
    </>
  )
}

/** 向导页容器：统一留白 + kicker/标题/引导语。宽屏统一 1080 自适应，两侧留白适度。 */
export function WizardPage({
  kicker,
  title,
  lead,
  children,
}: {
  kicker?: string
  title: string
  lead?: string
  children?: ReactNode
}) {
  return (
    <div className={`${PAGE_FRAME} pt-8 pb-6`}>
      <WizardHeading kicker={kicker} title={title} lead={lead} />
      <div className="mt-[18px]">{children}</div>
    </div>
  )
}

/** 定时向导共用的滚动和底栏布局。 */
export function WizardTimerPage({ children, footer }: { children: ReactNode; footer: ReactNode }) {
  return (
    <div className="flex h-full min-h-0 flex-col min-[1120px]:overflow-hidden">
      <div className="min-h-0 flex-1 overflow-y-auto min-[1120px]:overflow-hidden">
        <div className={`${PAGE_FRAME} h-full pt-8 pb-6 min-[1120px]:flex min-[1120px]:min-h-0 min-[1120px]:flex-col min-[1120px]:pb-4`}>
          <div className="min-[1120px]:min-h-0 min-[1120px]:flex-1">{children}</div>
        </div>
      </div>
      {footer}
    </div>
  )
}

/** 底部导航条：上一步 + 下一步（可自定义异步动作）。 */
export function WizardNav({
  prevTo,
  onPrev,
  nextLabel = '下一步',
  nextTo,
  onNext,
  nextDisabled,
  inline,
}: {
  prevTo?: string
  onPrev?: () => void
  nextLabel?: string
  nextTo?: string
  onNext?: () => void | Promise<void>
  nextDisabled?: boolean
  inline?: boolean
}) {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)

  const handleNext = async () => {
    if (nextDisabled || busy) return
    if (onNext) {
      setBusy(true)
      try {
        await onNext()
      } finally {
        setBusy(false)
      }
    } else if (nextTo) {
      navigate(nextTo)
    }
  }

  const nextCls = 'bg-ink-1 border-ink-1 text-surface font-[550]'

  const actions = (
    <>
      {prevTo || onPrev ? (
        <button
          className="cursor-pointer rounded-[11px] border border-line bg-surface px-[22px] py-2.5 text-[15px] text-ink-2"
          onClick={onPrev ?? (() => { if (prevTo) navigate(prevTo) })}
        >
          上一步
        </button>
      ) : (
        <span />
      )}
      {!inline && <span className="flex-1" />}
      <button
        className={`cursor-pointer rounded-[11px] border px-[22px] py-2.5 text-[15px] disabled:opacity-45 ${nextCls}`}
        onClick={handleNext}
        disabled={nextDisabled || busy}
      >
        {busy ? '处理中…' : nextLabel}
      </button>
    </>
  )

  if (inline) {
    return <div className="mt-auto flex shrink-0 flex-wrap justify-end gap-3 border-t border-line pt-4">{actions}</div>
  }

  return (
    <div className="shrink-0 border-t border-line bg-surface">
      <div className={PAGE_GUTTERS}>
        <div className="flex gap-3 py-3.5">{actions}</div>
      </div>
    </div>
  )
}
