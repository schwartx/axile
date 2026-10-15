import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { CalendarDays, ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react'
import { calendarKeyDay, formatDay, monthDays, monthWeeks, moveDay, moveMonth, parseDay } from '@/components/ui/datePickerModel'
import { pickRange, presetRange, rangeDays, type DateRange } from '@/components/ui/dateRangeModel'

interface Props {
  value: DateRange
  onChange: (value: DateRange) => void
  maxDays?: number
  describedBy?: string
  disabled?: boolean
}
const ACTION = 'inline-flex min-h-8 cursor-pointer items-center justify-center rounded-md px-2 text-xs text-ink-2 outline-none hover:bg-fill focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default disabled:opacity-30'
const INPUT = 'num min-w-0 w-full rounded-lg border border-line bg-surface px-2.5 py-2 text-[13px] text-ink-1 outline-none focus:border-accent disabled:opacity-50'

interface MonthProps {
  month: string
  weeks: number
  value: DateRange
  preview: DateRange
  active: string
  today: string
  limit: string | null
  onChoose: (day: string) => void
  onFocus: (day: string) => void
  onPreview: (day: string | null) => void
}

/** 邻月留空，双月模式下同一个日期只出现一次，省去整周空白。 */
function RangeMonth({ month, weeks, value, preview, active, today, limit, onChoose, onFocus, onPreview }: MonthProps) {
  const days = monthDays(month)
  const label = month.slice(0, 4) + ' 年 ' + Number(month.slice(5)) + ' 月'
  return <div className="min-w-0">
    <div className="mb-1 text-center text-[13px] font-medium text-ink-1">{label}</div>
    <table role="grid" aria-label={label} aria-multiselectable="true" className="w-full table-fixed border-separate border-spacing-y-0.5 text-center text-xs" onMouseLeave={() => onPreview(null)}>
      <thead><tr>{['一', '二', '三', '四', '五', '六', '日'].map(day => <th key={day} scope="col" className="h-6 font-normal text-ink-3">{day}</th>)}</tr></thead>
      <tbody>{Array.from({ length: weeks }, (_, week) => <tr key={week}>{days.slice(week * 7, week * 7 + 7).map((day, column) => {
        if (!day.startsWith(month)) return <td key={day} className="h-8 sm:h-7" />
        const selected = day === preview.from || day === preview.to
        const inRange = !!preview.to && day >= preview.from && day <= preview.to
        const committed = !!value.to && day >= value.from && day <= value.to
        return <td key={day} aria-selected={committed || day === value.from} className={'p-0 '
          + (inRange ? 'bg-accent-soft ' : '')
          + (day === preview.from || column === 0 ? 'rounded-l-md ' : '')
          + (day === preview.to || column === 6 ? 'rounded-r-md' : '')}>
          <button type="button" data-range-day={day} aria-label={day} aria-current={day === today ? 'date' : undefined}
            tabIndex={active === day ? 0 : -1} disabled={!!limit && day > limit}
            onClick={() => onChoose(day)} onFocus={() => { onFocus(day); onPreview(day) }} onMouseEnter={() => onPreview(day)}
            className={'num flex h-8 sm:h-7 min-w-0! w-full cursor-pointer items-center justify-center rounded-md outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-surface disabled:cursor-default disabled:opacity-25 '
              + (selected ? 'bg-accent text-surface' : inRange ? 'text-accent hover:bg-accent-soft' : 'text-ink-1 hover:bg-fill')}>
            <span className={day === today && !selected ? 'border-b border-accent' : ''}>{Number(day.slice(8))}</span>
          </button>
        </td>
      })}</tr>)}</tbody>
    </table>
  </div>
}

/** 整条范围入口 + 原位日历；选日、手动输入与快捷范围共用一个受控值。 */
export function DateRangePicker({ value, onChange, maxDays = 31, describedBy, disabled }: Props) {
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const manualInput = useRef<HTMLInputElement>(null)
  const focusWanted = useRef(false)
  const today = formatDay(Date.now() + 8 * 3600000)
  const [expanded, setExpanded] = useState(false)
  const [manualOpen, setManualOpen] = useState(false)
  const [anchor, setAnchor] = useState<string | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)
  const [month, setMonth] = useState(today.slice(0, 7))
  const [active, setActive] = useState(today)
  const count = rangeDays(value)
  const limit = anchor ? moveDay(anchor, maxDays - 1) : null
  const preview = anchor ? { from: anchor, to: hovered && hovered >= anchor && hovered <= limit! ? hovered : '' } : value
  const previewCount = anchor ? rangeDays(preview) : null
  const secondMonth = moveMonth(month + '-01', 1).slice(0, 7)
  const weeks = Math.max(monthWeeks(month), monthWeeks(secondMonth))

  const close = () => { setExpanded(false); setHovered(null); trigger.current?.focus({ preventScroll: true }) }
  const show = () => {
    const day = parseDay(value.from) != null ? value.from : today
    setMonth(day.slice(0, 7))
    setActive(day)
    setAnchor(parseDay(value.from) != null && !value.to ? value.from : null)
    setHovered(null)
    setManualOpen(false)
    setExpanded(true)
  }
  useLayoutEffect(() => {
    if (manualOpen) manualInput.current?.focus()
  }, [manualOpen])
  useLayoutEffect(() => {
    if (!expanded || !focusWanted.current) return
    focusWanted.current = false
    panel.current?.querySelector<HTMLButtonElement>('[data-range-day="' + active + '"]')?.focus({ preventScroll: true })
  }, [expanded, active, month])
  useEffect(() => {
    const media = window.matchMedia('(min-width: 640px)')
    const resize = () => {
      if (!media.matches) setMonth(active.slice(0, 7))
    }
    media.addEventListener('change', resize)
    return () => media.removeEventListener('change', resize)
  }, [active])

  const choose = (day: string) => {
    const next = pickRange(value, anchor, day, maxDays)
    onChange(next.value)
    setAnchor(next.anchor)
    setHovered(null)
    setActive(day)
    if (!next.anchor) close()
  }
  const manual = (field: keyof DateRange, text: string) => {
    setAnchor(null)
    setHovered(null)
    onChange({ ...value, [field]: text })
    if (parseDay(text) != null) { setMonth(text.slice(0, 7)); setActive(text) }
  }
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close()
      return
    }
    if (!(event.target instanceof HTMLElement)) return
    if (event.target.tagName === 'INPUT' && event.key === 'Enter') {
      event.preventDefault()
      if (count != null && count <= maxDays) close()
      return
    }
    const current = event.target.dataset.rangeDay
    if (!current) return
    const next = calendarKeyDay(current, event.key)
    if (!next) return
    event.preventDefault()
    const day = limit && next > limit ? limit : next
    setActive(day)
    const lastMonth = window.matchMedia('(min-width: 640px)').matches ? secondMonth : month
    if (day.slice(0, 7) < month || day.slice(0, 7) > lastMonth) setMonth(day.slice(0, 7))
    setHovered(day)
    focusWanted.current = true
  }
  const preset = (days: number, offset = 0) => {
    onChange(presetRange(today, days, offset))
    setAnchor(null)
    setHovered(null)
    setExpanded(false)
  }
  const navigateMonth = (offset: number) => {
    const next = moveMonth(active, offset)
    setActive(limit && next > limit ? limit : next)
    setMonth(moveMonth(month + '-01', offset).slice(0, 7))
    setHovered(null)
  }

  return <div className="min-w-0">
    <div className="mb-2 text-sm text-ink-2">日期范围</div>
    <button ref={trigger} type="button" aria-label="选择导出日期范围" aria-expanded={expanded} aria-controls={id} aria-describedby={describedBy}
      disabled={disabled} onClick={() => expanded ? close() : show()}
      onKeyDown={event => {
        if (event.key === 'ArrowDown') { event.preventDefault(); focusWanted.current = true; show() }
      }}
      className="flex min-h-11 w-full cursor-pointer items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-left outline-none hover:border-border-strong focus-visible:border-accent disabled:cursor-default disabled:opacity-50">
      <CalendarDays size={16} className="shrink-0 text-ink-3" aria-hidden />
      <span className="num flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-ink-1">
        <span>{value.from || '选择开始日期'}</span><span className="text-ink-3">→</span><span>{value.to || '选择结束日期'}</span>
      </span>
      {count != null && <span className={'num shrink-0 text-xs ' + (count > maxDays ? 'text-warn' : 'text-ink-3')}>{count} 天</span>}
      <ChevronDown size={14} aria-hidden className={'shrink-0 text-ink-3 transition-transform duration-200 motion-reduce:transition-none ' + (expanded ? 'rotate-180' : '')} />
    </button>
    <div className="mt-2 flex flex-wrap gap-1" aria-label="快捷日期范围">
      {([{ label: '今天', days: 1 }, { label: '昨天', days: 1, offset: -1 }, { label: '近 7 天', days: 7 }, { label: '近 30 天', days: 30 }]).map(option => {
        const range = presetRange(today, option.days, option.offset)
        const selected = range.from === value.from && range.to === value.to
        return <button key={option.label} type="button" disabled={disabled || option.days > maxDays} aria-pressed={selected}
          className={ACTION + (selected ? ' bg-accent-soft text-accent' : '')} onClick={() => preset(option.days, option.offset)}>{option.label}</button>
      })}
    </div>
    <div inert={!expanded} aria-hidden={!expanded} className={'grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ' + (expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]')}>
      <div className="min-h-0 overflow-hidden">
        <div ref={panel} id={id} role="region" aria-label="导出日期范围日历" onKeyDown={onKeyDown} className="mt-2 max-w-[560px] rounded-xl border border-line bg-bg-subtle p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <button type="button" aria-label="上个月" className={ACTION + ' px-1.5'} onClick={() => navigateMonth(-1)}><ChevronLeft size={16} aria-hidden /></button>
            <p role="status" className="text-center text-xs text-ink-2">{previewCount
              ? preview.from + ' → ' + preview.to + ' · ' + previewCount + ' 天（预览）'
              : anchor ? '选择结束日期 · 最多 ' + maxDays + ' 天' : '先选开始日期，再选结束日期'}</p>
            <button type="button" aria-label="下个月" className={ACTION + ' px-1.5'} disabled={!!limit && secondMonth > limit.slice(0, 7)} onClick={() => navigateMonth(1)}><ChevronRight size={16} aria-hidden /></button>
          </div>
          <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
            <RangeMonth month={month} weeks={weeks} value={value} preview={preview} active={active} today={today} limit={limit} onChoose={choose} onFocus={setActive} onPreview={setHovered} />
            <div className="hidden min-w-0 sm:block"><RangeMonth month={secondMonth} weeks={weeks} value={value} preview={preview} active={active} today={today} limit={limit} onChoose={choose} onFocus={setActive} onPreview={setHovered} /></div>
          </div>
          <div id={id + '-manual'} inert={!manualOpen} aria-hidden={!manualOpen} className={'grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ' + (manualOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]')}>
            <div className="min-h-0 overflow-hidden"><div className="mt-2 grid grid-cols-2 gap-3 border-t border-line pt-2">
            <label className="min-w-0 space-y-1.5 text-xs text-ink-2"><span className="block">开始日期</span>
              <input ref={manualInput} type="text" inputMode="numeric" aria-label="导出开始日期" aria-describedby={describedBy} autoComplete="off" placeholder="YYYY-MM-DD" value={value.from} disabled={disabled} onChange={event => manual('from', event.target.value)} className={INPUT} />
            </label>
            <label className="min-w-0 space-y-1.5 text-xs text-ink-2"><span className="block">结束日期</span>
              <input type="text" inputMode="numeric" aria-label="导出结束日期" aria-describedby={describedBy} autoComplete="off" placeholder="YYYY-MM-DD" value={value.to} disabled={disabled} onChange={event => manual('to', event.target.value)} className={INPUT} />
            </label>
            </div></div>
          </div>
          <div className="mt-2 flex justify-between gap-2 border-t border-line pt-1">
            <button type="button" className={ACTION} onClick={() => { onChange({ from: '', to: '' }); setAnchor(null); setHovered(null) }}>清空</button>
            <button type="button" className={ACTION} aria-expanded={manualOpen} aria-controls={id + '-manual'} onClick={() => setManualOpen(previous => !previous)}>手动输入</button>
            <button type="button" className={ACTION} onClick={close}>收起日历</button>
          </div>
        </div>
      </div>
    </div>
  </div>
}
