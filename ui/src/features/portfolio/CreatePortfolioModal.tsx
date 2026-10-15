import { useEffect, useRef, useState } from 'react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { OverflowText } from '@/components/ui/OverflowText'
import {
  portfolioMarketOptions,
  portfolioTemplate,
  type PortfolioMarketOption,
} from '@/features/portfolio/portfolioMarkets'
import { createPortfolio } from '@/lib/api/portfolios'
import { useChannelCatalogStore } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import type { Portfolio } from '@/types/api'

/** 市场选择卡片：创建时一次定死，此后在编辑器里只读展示。 */
function MarketChoiceGroup({
  value,
  options,
  onChange,
}: {
  value: string
  options: PortfolioMarketOption[]
  onChange: (value: string) => void
}) {
  return (
    <div role="radiogroup" aria-label="市场" className="grid gap-px overflow-hidden rounded-[8px] border border-line bg-line sm:grid-cols-3">
      {options.map((option) => {
        const selected = option.value === value
        const examples = option.exampleSymbols.slice(0, 2).join(' · ')
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            className={`relative flex min-h-[76px] min-w-0 items-center bg-surface px-4 py-3 text-left transition-colors ${
              selected ? 'bg-accent-soft' : 'hover:bg-bg-subtle'
            }`}
            onClick={() => onChange(option.value)}
          >
            <span className="min-w-0 flex-1">
              <span className={`block text-[15px] font-[620] ${selected ? 'text-ink-1' : 'text-ink-2'}`}>{option.label}</span>
              <OverflowText className="mt-1 text-[13px] text-ink-3" text={examples} />
            </span>
            {selected && <span aria-hidden className="absolute inset-y-0 left-0 w-0.5 bg-accent sm:inset-x-0 sm:top-auto sm:bottom-0 sm:h-0.5 sm:w-auto" />}
          </button>
        )
      })}
    </div>
  )
}

/**
 * 新建组合弹层：只定「组合是什么」（名称 + 市场），确认即以市场模板落库；
 * 代码是迭代物，留给编辑器工作台。市场是目标函数的定义域（模板、示例品种、
 * 试跑上下文都由它派生），因此创建后固定不可改。
 */
export function CreatePortfolioModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  /** 已创建的组合；由调用方决定去向（进编辑器 / 选入账户向导）。 */
  onCreated: (created: Portfolio) => void
}) {
  const channels = useChannelCatalogStore((state) => state.channels)
  const loading = useChannelCatalogStore((state) => state.loading)
  const catalogError = useChannelCatalogStore((state) => state.error)
  const refreshCatalog = useChannelCatalogStore((state) => state.refresh)
  const refreshPortfolios = useDomainStore((state) => state.refreshPortfolios)
  const toast = useToastStore((state) => state.toast)

  const markets = portfolioMarketOptions(channels ?? [])
  const [name, setName] = useState('')
  const [market, setMarket] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [createError, setCreateError] = useState<Error | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)

  const selected = markets.find((option) => option.value === market) ?? null
  const canCreate = Boolean(name.trim()) && selected != null && !submitting

  // 打开时回到空白草稿、聚焦名称、归还焦点给触发者；市场目录就绪后缺省选第一个。
  useEffect(() => {
    if (!open) return
    setName('')
    setMarket('')
    setCreateError(null)
    setSubmitting(false)
    const trigger = document.activeElement as HTMLElement | null
    nameRef.current?.focus({ preventScroll: true })
    return () => trigger?.focus?.({ preventScroll: true })
  }, [open])

  useEffect(() => {
    if (open && market === '' && markets.length > 0) setMarket(markets[0]!.value)
  }, [open, market, markets])

  // Escape 关闭（提交中不关）；Tab 在弹层内循环，不逃逸到背景。
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) {
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      if (!focusables || focusables.length === 0) return
      const first = focusables[0]!
      const last = focusables[focusables.length - 1]!
      const active = document.activeElement
      const inside = dialogRef.current?.contains(active)
      if (event.shiftKey && (active === first || !inside)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !inside)) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, submitting, onClose])

  const doCreate = async () => {
    if (!canCreate || !selected) return
    setSubmitting(true)
    setCreateError(null)
    try {
      const created = await createPortfolio({
        name: name.trim(),
        market: selected.label,
        custom_calc_py_code: portfolioTemplate(selected),
      })
      toast('组合已创建')
      void refreshPortfolios()
      onCreated(created)
    } catch (error) {
      setCreateError(error instanceof Error ? error : new Error(String(error)))
      setSubmitting(false)
    }
  }

  return (
    <>
      <div
        className={`fixed inset-0 z-[35] bg-scrim transition-opacity duration-150 ${
          open ? 'opacity-100' : 'pointer-events-none opacity-0'
        }`}
        onClick={submitting ? undefined : onClose}
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="新建组合"
        className={`fixed left-1/2 top-1/2 z-[36] w-[560px] max-w-[92vw] -translate-x-1/2 rounded-[18px] bg-surface shadow-[0_24px_60px_rgba(0,0,0,0.24)] transition-all duration-150 ${
          open ? '-translate-y-1/2 opacity-100' : 'pointer-events-none -translate-y-[46%] opacity-0'
        }`}
      >
        {open && (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void doCreate()
            }}
          >
            <div className="px-[22px] pt-5 pb-1 text-[18px] font-[640]">新建组合</div>
            <div className="px-[22px] pb-4 text-[14px] leading-relaxed text-ink-2">
              定好名称与市场即可创建；目标代码在编辑器里写，从市场模板起步。
            </div>
            <div className="max-h-[70vh] overflow-y-auto px-[22px]">
              <label className="mb-1.5 block text-[14px] text-ink-2" htmlFor="create-portfolio-name">
                组合名称
              </label>
              <input
                id="create-portfolio-name"
                ref={nameRef}
                className="w-full rounded-[11px] border border-ink-3/30 bg-surface px-3.5 py-3 text-[16px] outline-none focus:border-accent disabled:opacity-45"
                value={name}
                onChange={(event) => {
                  setName(event.target.value)
                  setCreateError(null)
                }}
                placeholder="例如：我的趋势组合"
                disabled={submitting}
              />
              <div className="mb-1.5 mt-5 flex items-baseline justify-between">
                <label className="block text-[14px] text-ink-2">市场</label>
                <span className="text-[12.5px] text-ink-3">创建后固定，不可更改</span>
              </div>
              <MarketChoiceGroup
                value={market}
                options={markets}
                onChange={(value) => {
                  setMarket(value)
                  setCreateError(null)
                }}
              />
              {loading && markets.length === 0 && <p className="mt-2 text-[14px] text-ink-2">加载市场…</p>}
              <ErrorNotice intent="read" embedded title="市场目录加载失败" error={markets.length === 0 ? catalogError : null} onRetry={refreshCatalog} />
              <ErrorNotice intent="mutation" embedded title="创建组合失败" error={createError} variant="mutation" onRetry={() => void doCreate()} />
            </div>
            <div className="mt-4 flex justify-end gap-2.5 border-t border-line px-5 py-3.5">
              <button
                type="button"
                className="inline-flex cursor-pointer items-center rounded-[9px] border border-line bg-surface px-4 py-2 text-sm text-ink-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/55 focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:opacity-45"
                onClick={onClose}
                disabled={submitting}
              >
                取消
              </button>
              <button
                type="submit"
                className="inline-flex cursor-pointer items-center rounded-[9px] border-0 bg-ink-1 px-[18px] py-2 text-sm font-[550] text-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/55 focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-default disabled:opacity-45"
                disabled={!canCreate}
              >
                {submitting ? '创建中…' : '创建并编辑'}
              </button>
            </div>
          </form>
        )}
      </div>
    </>
  )
}
