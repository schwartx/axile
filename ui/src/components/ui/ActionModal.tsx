import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { ModalContentContext } from '@/components/ui/modalContext'

/** 仅承载快捷动作的弹层；无可见标题，点击遮罩或 Escape 取消。 */
export function ActionModal({ open, label, onClose, children }: {
  open: boolean
  label: string
  onClose: () => void
  children: ReactNode
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  useEffect(() => { closeRef.current = onClose }, [onClose])
  useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    const panel = panelRef.current
    // 焦点落在外壳，避免打开时的回车直接提交某个动作。
    panel?.focus({ preventScroll: true })
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        closeRef.current()
      }
      if (event.key !== 'Tab') return
      const buttons = [...(panel?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])]
      const first = buttons[0], last = buttons.at(-1)
      if (!first) { event.preventDefault(); panel?.focus(); return }
      const outside = document.activeElement === panel || !panel?.contains(document.activeElement)
      if (event.shiftKey && (outside || document.activeElement === first)) {
        event.preventDefault(); last?.focus()
      } else if (!event.shiftKey && (outside || document.activeElement === last)) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      previous?.focus({ preventScroll: true })
    }
  }, [open])
  if (!open) return null
  return createPortal(<ModalContentContext.Provider value={true}>
    <div className="fixed inset-0 z-[35] flex items-center justify-center bg-scrim p-4" onClick={(event) => { event.stopPropagation(); if (event.target === event.currentTarget) onClose() }}>
      <div ref={panelRef} role="dialog" aria-modal="true" aria-label={label} tabIndex={-1}
        className="flex w-[360px] max-w-full flex-col gap-3 rounded-2xl bg-surface p-3 text-ink-1 shadow-[0_24px_60px_rgba(0,0,0,0.24)] outline-none">
        {children}
      </div>
    </div>
  </ModalContentContext.Provider>, document.body)
}
