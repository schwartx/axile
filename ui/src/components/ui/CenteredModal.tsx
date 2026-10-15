import { ModalContentContext } from '@/components/ui/modalContext'
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/** 居中弹窗共用外壳：遮罩、开关过渡、标题、关闭图标与焦点管理。 */
export function CenteredModal({ open, title, subtitle, ariaLabel, onClose, closeDisabled = false, restoreFocus = true, widthClassName = 'w-[480px]', footer, children }: {
  open: boolean
  title: string
  subtitle?: ReactNode
  ariaLabel?: string
  onClose: () => void
  closeDisabled?: boolean
  /** 调用方按打开方式决定是否恢复入口焦点，不由关闭时的按键推断。 */
  restoreFocus?: boolean
  widthClassName?: string
  footer?: ReactNode
  children: ReactNode
}) {
  const titleId = useId()
  const [present, setPresent] = useState(open)
  useLayoutEffect(() => {
    if (open) { setPresent(true); return }
    const timer = window.setTimeout(() => setPresent(false), 150)
    return () => window.clearTimeout(timer)
  }, [open])
  const panelRef = useRef<HTMLDivElement>(null)
  const handlers = useRef({ onClose, closeDisabled })
  useEffect(() => { handlers.current = { onClose, closeDisabled } }, [onClose, closeDisabled])
  useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    panelRef.current?.focus({ preventScroll: true })
    // visibility 过渡的首帧可能拒绝 focus；下一帧补入，但不打断已进入弹窗的操作。
    const focusFrame = window.requestAnimationFrame(() => {
      const panel = panelRef.current
      if (panel && !panel.contains(document.activeElement)) panel.focus({ preventScroll: true })
    })
    const keydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        if (!handlers.current.closeDisabled) handlers.current.onClose()
        return
      }
      if (event.key !== 'Tab') return
      const panel = panelRef.current
      const items = [...(panel?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])') ?? [])]
        .filter((item) => !item.closest('[inert]') && item.getClientRects().length > 0)
      const first = items[0], last = items.at(-1)
      if (!first) { event.preventDefault(); panel?.focus(); return }
      const outside = !panel?.contains(document.activeElement) || document.activeElement === panel
      if (event.shiftKey && (outside || document.activeElement === first)) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && (outside || document.activeElement === last)) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', keydown)
    return () => {
      window.cancelAnimationFrame(focusFrame)
      document.removeEventListener('keydown', keydown)
      if (restoreFocus) previous?.focus({ preventScroll: true })
      else previous?.blur()
    }
  }, [open, restoreFocus])
  const close = () => { if (!closeDisabled) onClose() }
  return createPortal(<ModalContentContext.Provider value={true}><div onClick={(event) => event.stopPropagation()}>
    <div
      aria-hidden="true"
      className={`fixed inset-0 z-[35] bg-scrim transition-opacity duration-150 motion-reduce:transition-none ${open ? 'opacity-100' : 'pointer-events-none opacity-0'}`}
      onClick={close}
    />
    <div
      ref={panelRef}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      aria-labelledby={ariaLabel ? undefined : titleId}
      aria-hidden={!open}
      inert={!open}
      className={`fixed left-1/2 top-1/2 z-[36] flex max-h-[86vh] ${widthClassName} max-w-[92vw] -translate-x-1/2 flex-col rounded-[18px] bg-surface text-ink-1 shadow-[0_24px_60px_rgba(0,0,0,0.24)] outline-none transition-all duration-150 motion-reduce:transition-none ${open ? 'visible -translate-y-1/2 opacity-100' : 'pointer-events-none invisible -translate-y-[46%] opacity-0'}`}
    >
      <div className="flex items-start justify-between px-[22px] pt-5 pb-3">
        <div className="min-w-0">
          <h2 id={titleId} className="text-[18px] font-[640] [overflow-wrap:anywhere]">{title}</h2>
          {subtitle && <div className="mt-1 text-[14px] text-ink-2">{subtitle}</div>}
        </div>
        <button type="button" className="cursor-pointer text-[21px] leading-none text-ink-3 hover:text-ink-1 disabled:cursor-default" onClick={close} disabled={closeDisabled} aria-label="关闭">✕</button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-[22px] pb-2">{present && children}</div>
      {present && footer}
    </div>
  </div></ModalContentContext.Provider>, document.body)
}
