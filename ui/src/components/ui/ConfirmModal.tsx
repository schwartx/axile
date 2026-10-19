import { useEffect, useRef } from 'react'

export interface ConfirmSpec {
  title: string
  body: string
  /** 确认按钮文案。 */
  okText: string
  /** 危险操作用琥珀色文字和边框提示。 */
  danger?: boolean
  primary?: boolean
  onConfirm: () => void
}

/** 居中确认弹窗。传入 spec 打开，null 关闭。 */
export function ConfirmModal({
  spec,
  onClose,
}: {
  spec: ConfirmSpec | null
  onClose: () => void
}) {
  const open = spec != null
  // 危险操作的默认焦点落在「取消」上：回车走安全路，琥珀色确认必须手点，摩擦才留得住。
  const danger = spec?.danger ?? false
  const dialogRef = useRef<HTMLDivElement>(null)
  const okRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  // 记住打开前的焦点，关闭时归还给触发它的那个按钮（否则焦点掉回 body）。
  const triggerRef = useRef<HTMLElement | null>(null)

  // Escape 关闭 + Tab 焦点陷阱（只在「取消 / 确认」两颗按钮间循环，不逃逸到背景）。
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key !== 'Tab') return
      const first = cancelRef.current
      const last = okRef.current
      if (!first || !last) return
      const active = document.activeElement
      const inside = dialogRef.current?.contains(active)
      if (e.shiftKey && (active === first || !inside)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (active === last || !inside)) {
        e.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // 打开时把焦点放到「默认按钮」上（危险→取消、良性→确认）：浏览器原生「回车=激活聚焦按钮」，
  // 于是回车自动接对；关闭时把焦点还给触发者。
  useEffect(() => {
    if (!open) return
    triggerRef.current = document.activeElement as HTMLElement | null
    const def = danger ? cancelRef.current : okRef.current
    def?.focus({ preventScroll: true })
    return () => {
      triggerRef.current?.focus?.({ preventScroll: true })
    }
  }, [open, danger])

  return (
    <>
      <div
        className={`fixed inset-0 z-[35] bg-scrim transition-opacity duration-150 ${
          open ? 'opacity-100' : 'pointer-events-none opacity-0'
        }`}
        onClick={onClose}
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        className={`fixed left-1/2 top-1/2 z-[36] w-[420px] max-w-[92vw] -translate-x-1/2 rounded-[18px] bg-surface shadow-[0_24px_60px_rgba(0,0,0,0.24)] transition-all duration-150 ${
          open ? '-translate-y-1/2 opacity-100' : 'pointer-events-none -translate-y-[46%] opacity-0'
        }`}
      >
        {spec && (
          <>
            <div className="px-[22px] pt-5 pb-1.5 text-[18px] font-[640]">{spec.title}</div>
            <div className="px-[22px] pb-[18px] text-[14.5px] leading-relaxed text-ink-2">
              {spec.body}
            </div>
            <div className="flex justify-end gap-2.5 border-t border-line px-5 py-3.5">
              <button
                ref={cancelRef}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-[9px] border border-line bg-surface px-4 py-2 text-sm text-ink-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink-2/55 focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                onClick={onClose}
              >
                取消
                {danger && <span aria-hidden className="text-[13px] leading-none opacity-55">⏎</span>}
              </button>
              <button
                ref={okRef}
                className={`inline-flex cursor-pointer items-center gap-1.5 rounded-[9px] border px-[18px] py-2 text-sm font-[550] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-surface ${
                  spec.primary
                    ? 'border-transparent bg-ink-1 text-surface focus-visible:ring-accent/55'
                    : spec.danger
                    ? 'border-warn/55 bg-surface text-warn hover:bg-warn-soft focus-visible:ring-warn/60'
                    : 'border-transparent bg-ink-1 text-surface focus-visible:ring-accent/55'
                }`}
                onClick={() => {
                  onClose()
                  spec.onConfirm()
                }}
              >
                {spec.okText}
                {!danger && <span aria-hidden className="text-[13px] leading-none opacity-55">⏎</span>}
              </button>
            </div>
          </>
        )}
      </div>
    </>
  )
}
