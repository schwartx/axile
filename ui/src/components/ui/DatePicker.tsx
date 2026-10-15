import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react'
import { calendarKeyDay, clampDay, formatDay, monthDays, moveMonth, parseDay } from '@/components/ui/datePickerModel'

interface Props {
  value: string
  onChange: (value: string) => void
  ariaLabel: string
  describedBy?: string
  min?: string
  max?: string
  disabled?: boolean
  className?: string
}
const ICON_BUTTON = 'inline-flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-md text-ink-3 outline-none hover:bg-fill hover:text-ink-1 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default disabled:opacity-30'

/** 主题日历：popover 顶层避免裁剪，DOM 留在原表单中，沿用弹窗的焦点管理。 */
export function DatePicker({ value, onChange, ariaLabel, describedBy, min, max, disabled, className = '' }: Props) {
  const id = useId()
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const focusWanted = useRef(false)
  const [open, setOpen] = useState(false)
  const today = formatDay(Date.now() + 8 * 3600000)
  const [active, setActive] = useState(today)
  const [month, setMonth] = useState(today.slice(0, 7))
  const monthLabel = month.slice(0, 4) + ' 年 ' + Number(month.slice(5)) + ' 月'
  const days = monthDays(month)
  const allowed = (day: string) => parseDay(day) != null && (!min || parseDay(min) == null || day >= min) && (!max || parseDay(max) == null || day <= max)

  const place = useCallback(() => {
    if (!root.current || !panel.current) return
    const anchor = root.current.getBoundingClientRect()
    const popup = panel.current
    const width = Math.min(284, window.innerWidth - 24)
    popup.style.width = width + 'px'
    popup.style.maxHeight = (window.innerHeight - 24) + 'px'
    const height = popup.offsetHeight
    const below = anchor.bottom + 6
    const top = below + height <= window.innerHeight - 12 ? below : anchor.top - height - 6
    popup.style.left = Math.max(12, Math.min(anchor.left, window.innerWidth - width - 12)) + 'px'
    popup.style.top = Math.max(12, Math.min(top, window.innerHeight - height - 12)) + 'px'
  }, [])

  const close = (refocus = false) => {
    panel.current?.hidePopover()
    setOpen(false)
    if (refocus) trigger.current?.focus()
  }
  const show = () => {
    // fieldset.disabled 也必须生效，不能只检查组件自己的 disabled。
    if (disabled || trigger.current?.matches(':disabled')) return
    const day = clampDay(parseDay(value) != null ? value : today, min, max)
    setActive(day)
    setMonth(day.slice(0, 7))
    focusWanted.current = true
    panel.current?.showPopover()
    setOpen(true)
  }
  useLayoutEffect(() => {
    if (!open) return
    place()
    if (focusWanted.current) {
      focusWanted.current = false
      panel.current?.querySelector<HTMLButtonElement>('[data-day="' + active + '"]')?.focus({ preventScroll: true })
    }
  }, [open, active, month, place])
  useEffect(() => {
    if (!open) return
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open, place])

  const choose = (day: string) => { onChange(day); close(true) }
  const navigateMonth = (offset: number) => {
    const day = clampDay(moveMonth(active, offset), min, max)
    focusWanted.current = true
    setActive(day)
    setMonth(day.slice(0, 7))
  }
  const monthAvailable = (offset: number) => monthDays(moveMonth(month + '-01', offset).slice(0, 7))
    .some(day => day.slice(0, 7) === moveMonth(month + '-01', offset).slice(0, 7) && allowed(day))
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close(true)
      return
    }
    if (!(event.target instanceof HTMLElement) || !event.target.dataset.day) return
    const next = calendarKeyDay(active, event.key)
    if (!next) return
    event.preventDefault()
    const day = clampDay(next, min, max)
    focusWanted.current = true
    setActive(day)
    setMonth(day.slice(0, 7))
  }

  return <div ref={root} className={'relative flex min-w-0 items-center rounded-lg border border-line bg-surface text-ink-1 focus-within:border-accent ' + className}
    onBlurCapture={event => {
      // 翻到边界月会禁用当前翻月按钮，浏览器此时可能给出 null 焦点。
      // 真正移出组件才关闭；点击空白由原生 popover 的 light-dismiss 处理。
      if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) close()
    }}>
    <input type="text" inputMode="numeric" value={value} placeholder="YYYY-MM-DD" disabled={disabled}
      aria-label={ariaLabel} aria-describedby={describedBy} autoComplete="off" spellCheck={false}
      onChange={event => onChange(event.target.value)}
      onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); show() } }}
      className="num min-w-0 w-full bg-transparent px-3 py-2 text-[13px] text-ink-1 outline-none placeholder:text-ink-3 disabled:opacity-50" />
    <button ref={trigger} type="button" disabled={disabled} aria-label={ariaLabel + '，打开日历'}
      aria-haspopup="dialog" aria-expanded={open} aria-controls={id}
      className={ICON_BUTTON + ' mr-1'} onClick={() => open ? close() : show()}><CalendarDays size={16} aria-hidden /></button>
    <div ref={panel} id={id} popover="auto" role="dialog" aria-label={ariaLabel + '日历'}
      onToggle={event => setOpen(event.newState === 'open')} onKeyDown={onKeyDown}
      className="fixed inset-auto m-0 overflow-y-auto rounded-xl border border-line bg-surface p-3 text-ink-1 shadow-card">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span aria-live="polite" className="pl-1 text-[13px] font-medium">{monthLabel}</span>
        <div className="flex gap-1">
          <button type="button" aria-label="上个月" className={ICON_BUTTON} disabled={!monthAvailable(-1)} onClick={() => navigateMonth(-1)}><ChevronLeft size={16} aria-hidden /></button>
          <button type="button" aria-label="下个月" className={ICON_BUTTON} disabled={!monthAvailable(1)} onClick={() => navigateMonth(1)}><ChevronRight size={16} aria-hidden /></button>
        </div>
      </div>
      <table role="grid" aria-label={monthLabel} className="w-full table-fixed border-separate border-spacing-0 text-center text-xs">
        <thead><tr>{['一', '二', '三', '四', '五', '六', '日'].map(day => <th key={day} scope="col" className="h-8 font-normal text-ink-3">{day}</th>)}</tr></thead>
        <tbody>{Array.from({ length: 6 }, (_, week) => <tr key={week}>{days.slice(week * 7, week * 7 + 7).map(day => <td key={day} aria-selected={day === value} className="p-0.5">
          <button type="button" data-day={day} aria-label={day} aria-current={day === today ? 'date' : undefined}
            tabIndex={day === active ? 0 : -1} disabled={!allowed(day)}
            onFocus={() => setActive(day)} onClick={() => choose(day)}
            className={'num flex h-8 min-w-0! w-full cursor-pointer items-center justify-center rounded-md outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-surface disabled:cursor-default disabled:opacity-25 '
              + (day === value ? 'bg-accent-soft font-medium text-accent' : 'hover:bg-fill ' + (day.slice(0, 7) === month ? 'text-ink-1' : 'text-ink-3'))}>
            <span className={day === today && day !== value ? 'border-b border-accent' : ''}>{Number(day.slice(8))}</span>
          </button>
        </td>)}</tr>)}</tbody>
      </table>
      <div className="mt-2 flex items-center justify-between border-t border-line pt-2 text-xs">
        <button type="button" className="cursor-pointer rounded-md px-2 py-1.5 text-ink-3 outline-none hover:bg-fill hover:text-ink-1 focus-visible:ring-2 focus-visible:ring-accent" onClick={() => choose('')}>清空</button>
        <button type="button" disabled={!allowed(today)} className="cursor-pointer rounded-md px-2 py-1.5 text-accent outline-none hover:bg-accent-soft focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default disabled:opacity-30" onClick={() => choose(today)}>今天</button>
      </div>
    </div>
  </div>
}
