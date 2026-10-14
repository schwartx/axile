import { useEffect, useRef, useState, type ComponentProps, type ReactNode } from 'react'
import { RadioTower, X } from 'lucide-react'
import { ConnectionValidation } from '@/features/account/connectionValidation/ConnectionValidation'

/** 宽屏固定在表单右侧；窄屏从右侧展开同一组件，验证状态不会因展开收起丢失。 */
export function ConnectionValidationAside({ children, ...props }: ComponentProps<typeof ConnectionValidation> & { children?: (trigger: ReactNode) => ReactNode }) {
  const [open, setOpen] = useState(false)
  const [wide, setWide] = useState(() => window.matchMedia('(min-width: 1280px)').matches)
  const panel = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const query = window.matchMedia('(min-width: 1280px)')
    const change = () => { setWide(query.matches) }
    query.addEventListener('change', change)
    return () => query.removeEventListener('change', change)
  }, [])
  useEffect(() => {
    if (!open || wide) return
    const previous = document.activeElement as HTMLElement | null
    panel.current?.focus({ preventScroll: true })
    const keyboard = (event: KeyboardEvent) => {
      // 验证按钮禁用时浏览器可能把焦点退回 body，仍允许 Escape 关闭面板。
      if (!panel.current || event.defaultPrevented || (!panel.current.contains(document.activeElement) && document.activeElement !== document.body)) return
      if (event.key === 'Escape') {
        event.preventDefault()
        setOpen(false)
      }
      if (event.key !== 'Tab') return
      const items = [...panel.current.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input:not(:disabled), [tabindex="0"]')]
        .filter(item => !item.closest('[inert]') && item.getClientRects().length > 0)
      const first = items[0], last = items.at(-1)
      if (!first) { event.preventDefault(); return }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
        event.preventDefault(); last?.focus()
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener('keydown', keyboard)
    return () => {
      document.removeEventListener('keydown', keyboard)
      previous?.focus({ preventScroll: true })
    }
  }, [open, wide])
  const triggerButton = <button ref={trigger} type="button" onClick={() => setOpen(true)} aria-expanded={open} aria-haspopup="dialog"
      className="flex shrink-0 cursor-pointer items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-2 text-[13px] text-ink-2 xl:hidden">
      <RadioTower size={15} aria-hidden="true" />连接验证
    </button>
  return <>
    {children?.(triggerButton)}
    <aside className="absolute right-0 top-0 z-30 min-w-0 xl:sticky xl:top-8 xl:z-auto">
    {!children && triggerButton}
    <div aria-hidden="true" onClick={() => setOpen(false)} className={`fixed inset-0 bg-scrim transition-opacity duration-200 motion-reduce:transition-none xl:hidden ${open ? 'opacity-100' : 'pointer-events-none opacity-0'}`} />
    <div ref={panel} tabIndex={wide ? undefined : -1} role={wide ? undefined : 'dialog'} aria-modal={wide ? undefined : true}
      aria-label={wide ? undefined : '账户连接验证面板'} inert={!wide && !open}
      className={`fixed inset-y-0 right-0 flex w-[360px] max-w-[92vw] flex-col overflow-y-auto bg-surface p-4 outline-none transition-transform duration-200 motion-reduce:transition-none xl:static xl:w-auto xl:max-w-none xl:overflow-visible xl:bg-transparent xl:p-0 xl:transition-none ${open ? 'translate-x-0' : 'translate-x-full'} xl:translate-x-0`}>
      <div className="mb-3 flex justify-end xl:hidden">
        <button type="button" onClick={() => setOpen(false)} aria-label="关闭连接验证" className="cursor-pointer rounded-lg p-2 text-ink-2"><X size={18} aria-hidden="true" /></button>
      </div>
      <ConnectionValidation {...props} />
    </div>
  </aside>
  </>
}
