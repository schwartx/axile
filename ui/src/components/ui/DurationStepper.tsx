import NumberFlow, { NumberFlowGroup } from '@number-flow/react'
import { useRef, useState, type KeyboardEvent } from 'react'
import { joinHourDuration, splitHourDuration, type DayHourParts } from '@/components/ui/duration'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { stepNumericValue } from '@/components/ui/numericStepper'

const UNITS = [{ key: 'days', label: '天', step: 24 }, { key: 'hours', label: '小时', step: 1 }] as const
const FORMAT = { useGrouping: false, maximumFractionDigits: 0 }
const SPIN = { duration: 220, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' }
const TRANSFORM = { duration: 180, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const OPACITY = { duration: 160, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }

/** 共用双步长和数字动效；总小时数按天小时展示，总天数支持空值标签。 */
export function DurationStepper({ value, onChange, min = 0, max, disabled = false, invalid = false, ariaLabel, describedBy, unit = 'hours', emptyLabel }: {
  unit?: 'hours' | 'days'
  emptyLabel?: string
  value: string
  onChange: (value: string) => void
  min?: number
  max?: number
  disabled?: boolean
  invalid?: boolean
  ariaLabel: string
  describedBy?: string
}) {
  const split = (source: string): DayHourParts => unit === 'days' ? { days: source, hours: '0' } : splitHourDuration(source)
  const empty = emptyLabel !== undefined && value.trim() === ''
  const [focused, setFocused] = useState<keyof DayHourParts | null>(null)
  const [draft, setDraft] = useState(() => ({ source: value, parts: split(value) }))
  if (draft.source !== value) setDraft({ source: value, parts: split(value) })
  const total = Number(value)
  const valid = empty || (value.trim() !== '' && Number.isSafeInteger(total) && total >= 0)
  const bad = invalid || !valid || (!empty && (total < min || (max !== undefined && total > max)))
  const previous = useRef({ total, trend: 1 })
  if (valid && !empty && total !== previous.current.total)
    previous.current = { total, trend: total < previous.current.total ? -1 : 1 }
  const stepped = (direction: -1 | 1, step: number) => {
    if (empty) return direction > 0 ? String(step) : value
    const next = stepNumericValue(value, direction, { step, min: emptyLabel === undefined ? min : 0, max })
    return emptyLabel !== undefined && next === '0' ? '' : next
  }
  const apply = (next: string) => {
    if (disabled || bad || next === value) return
    setDraft({ source: next, parts: split(next) })
    onChange(next)
  }
  const edit = (key: keyof DayHourParts, part: string) => {
    const parts = { ...draft.parts, [key]: part }
    const source = unit === 'days' ? part : joinHourDuration(parts)
    setFocused(key)
    setDraft({ source, parts })
    onChange(source)
  }
  const keyDown = (event: KeyboardEvent<HTMLInputElement>, step: number) => {
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault()
      apply(stepped(event.key === 'ArrowUp' ? 1 : -1, step))
    } else if (event.key === 'Enter' && !bad) {
      event.preventDefault()
      event.currentTarget.blur()
    }
  }
  const button = 'grid h-full w-9 shrink-0 cursor-pointer place-items-center text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-ink-3'
  const stepButton = (direction: -1 | 1, fast: boolean) => {
    const amount = fast ? (unit === 'days' ? 7 : 1) : 1
    const next = stepped(direction, fast ? (unit === 'days' ? 7 : 24) : 1)
    const action = direction > 0 ? '增加' : '减少'
    const label = unit === 'days' || fast ? '天' : '小时'
    return <button type="button" className={`${button} ${fast ? 'text-[13px]' : 'text-[19px]'} ${direction > 0 ? 'border-l' : 'border-r'} border-line`}
      disabled={disabled || bad || next === value} aria-label={`${ariaLabel}：${action} ${amount} ${label}`}
      title={`${action} ${amount} ${label}`} onClick={() => { setFocused(null); apply(next) }}>{direction > 0 ? '+' : '−'}{fast ? `${amount}天` : ''}</button>
  }
  return <div role="group" aria-label={`${ariaLabel}输入`} data-duration-stepper
    onBlur={event => {
      if (event.relatedTarget instanceof HTMLInputElement && event.currentTarget.contains(event.relatedTarget)) return
      setFocused(null)
      if (valid) setDraft({ source: value, parts: split(value) })
    }}
    className={`inline-flex h-10 shrink-0 items-center overflow-hidden rounded-[9px] border bg-surface ${disabled ? 'opacity-40' : ''} ${bad ? 'border-warn focus-within:border-warn' : 'border-ink-3/30 contrast:border-border-strong focus-within:border-accent'}`}>
    {stepButton(-1, true)}
    {stepButton(-1, false)}
    <NumberFlowGroup>
      <div className="flex h-full items-center px-1 text-[16px]">
        {(unit === 'days' ? UNITS.slice(0, 1) : UNITS).map(({ key, label, step }) => {
          const part = draft.parts[key]
          const editing = focused === key || bad
          const number = <NumberFlow value={empty ? previous.current.total : Number(part) || 0} className="text-ink-1" locales="zh-CN" format={FORMAT} trend={previous.current.trend}
            spinTiming={SPIN} transformTiming={TRANSFORM} opacityTiming={OPACITY} respectMotionPreference />
          const shown = unit === 'days' || focused !== null || bad || Number(part) !== 0
            || (key === 'hours' && Number(draft.parts.days) === 0)
          return <span key={key} data-duration-unit={key} inert={!shown} aria-hidden={!shown}
            className={`grid transition-[grid-template-columns] duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${shown ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]'}`}>
            <span className="min-w-0 overflow-hidden">
              <label className={`flex h-8 cursor-text items-center gap-1 rounded-md px-1.5 ${focused === key ? 'bg-fill' : ''}`}>
                <span className="relative block num" style={{ width: `${Math.max(unit === 'days' ? 6 : 2, Math.min(16, part.length))}ch` }}>
                  <span aria-hidden className={`pointer-events-none absolute inset-0 flex items-center gap-1 ${unit === 'days' ? 'justify-center' : 'justify-end'} ${editing ? 'invisible' : ''}`}>
                    {unit === 'days'
                      ? <InkRewrite text={empty ? emptyLabel! : label} className="place-items-center" textClassName="text-ink-3"
                        renderText={text => text === label
                          ? <span className="inline-flex items-baseline gap-1">{number}<span className="text-[13px]">{label}</span></span>
                          : text} />
                      : number}
                  </span>
                  <input aria-label={unit === 'days' ? ariaLabel : `${ariaLabel}：${label}`} placeholder={emptyLabel} aria-describedby={describedBy} aria-invalid={bad || undefined}
                    inputMode="numeric" autoComplete="off" disabled={disabled} value={part}
                    className={`block w-full border-0 bg-transparent p-0 ${unit === 'days' ? 'text-center' : 'text-right'} text-ink-1 outline-none ${editing ? '' : 'opacity-0'}`}
                    onChange={event => edit(key, event.target.value)}
                    onFocus={event => { setFocused(key); event.currentTarget.select() }} onKeyDown={event => keyDown(event, unit === 'days' ? 1 : step)} />
                </span>
                {unit !== 'days' && <span className="text-[13px] text-ink-3">{label}</span>}
              </label>
            </span>
          </span>
        })}
      </div>
    </NumberFlowGroup>
    {stepButton(1, false)}
    {stepButton(1, true)}
    <span className="sr-only" aria-live="polite" aria-atomic="true">{focused === null && valid ? (empty ? emptyLabel : unit === 'days' ? `${value}天` : `${draft.parts.days}天${draft.parts.hours}小时`) : ''}</span>
  </div>
}
