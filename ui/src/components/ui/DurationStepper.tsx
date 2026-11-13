import NumberFlow, { NumberFlowGroup } from '@number-flow/react'
import { useRef, useState, type KeyboardEvent } from 'react'
import { joinHourDuration, splitHourDuration, type DayHourParts } from '@/components/ui/duration'
import { stepNumericValue } from '@/components/ui/numericStepper'

const UNITS = [{ key: 'days', label: '天', step: 24 }, { key: 'hours', label: '小时', step: 1 }] as const
const FORMAT = { useGrouping: false, maximumFractionDigits: 0 }
const SPIN = { duration: 220, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' }
const TRANSFORM = { duration: 180, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const OPACITY = { duration: 160, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }

/** 天、小时多级步进：聚焦分段选择步长，进退位共享总小时数的滚动方向。 */
export function DurationStepper({ value, onChange, min = 0, max, disabled = false, invalid = false, ariaLabel, describedBy }: {
  value: string
  onChange: (value: string) => void
  min?: number
  max?: number
  disabled?: boolean
  invalid?: boolean
  ariaLabel: string
  describedBy?: string
}) {
  const [active, setActive] = useState<keyof DayHourParts>('hours')
  const [focused, setFocused] = useState<keyof DayHourParts | null>(null)
  const [draft, setDraft] = useState(() => ({ source: value, parts: splitHourDuration(value) }))
  if (draft.source !== value) setDraft({ source: value, parts: splitHourDuration(value) })
  const total = Number(value)
  const valid = value.trim() !== '' && Number.isSafeInteger(total) && total >= 0
  const bad = invalid || !valid || total < min || (max !== undefined && total > max)
  const previous = useRef({ total, trend: 1 })
  if (valid && total !== previous.current.total)
    previous.current = { total, trend: total < previous.current.total ? -1 : 1 }
  const unit = UNITS.find(unit => unit.key === active)!
  const decrease = stepNumericValue(value, -1, { step: unit.step, min, max })
  const increase = stepNumericValue(value, 1, { step: unit.step, min, max })
  const apply = (next: string) => {
    if (disabled || bad || next === value) return
    setFocused(null)
    setDraft({ source: next, parts: splitHourDuration(next) })
    onChange(next)
  }
  const edit = (key: keyof DayHourParts, part: string) => {
    const parts = { ...draft.parts, [key]: part }
    const source = joinHourDuration(parts)
    setFocused(key)
    setDraft({ source, parts })
    onChange(source)
  }
  const keyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault()
      apply(event.key === 'ArrowUp' ? increase : decrease)
    } else if (event.key === 'Enter' && !bad) {
      event.preventDefault()
      event.currentTarget.blur()
    }
  }
  const button = 'grid h-full w-9 shrink-0 cursor-pointer place-items-center text-[19px] text-ink-3 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-ink-3'
  return <div role="group" aria-label={`${ariaLabel}输入`} data-duration-stepper
    onBlur={event => {
      if (event.relatedTarget instanceof HTMLInputElement && event.currentTarget.contains(event.relatedTarget)) return
      setFocused(null)
      if (valid) setDraft({ source: value, parts: splitHourDuration(value) })
    }}
    className={`inline-flex h-10 shrink-0 items-center overflow-hidden rounded-[9px] border bg-surface ${disabled ? 'opacity-40' : ''} ${bad ? 'border-warn focus-within:border-warn' : 'border-ink-3/30 contrast:border-border-strong focus-within:border-accent'}`}>
    <button type="button" className={`${button} border-r border-line`} disabled={disabled || bad || decrease === value}
      aria-label={`${ariaLabel}：减少 1 ${unit.label}`} title={`减少 1 ${unit.label}`} onClick={() => apply(decrease)}>−</button>
    <NumberFlowGroup>
      <div className="flex h-full items-center gap-0.5 px-1 text-[16px]">
        {UNITS.map(({ key, label }) => {
          const part = draft.parts[key]
          const editing = focused === key || bad
          return <label key={key} className={`flex h-8 cursor-text items-center gap-1 rounded-md px-1.5 ${active === key ? 'bg-fill' : ''}`}>
            <span className="relative block num" style={{ width: `${Math.max(2, Math.min(16, part.length))}ch` }}>
              <span aria-hidden className={`pointer-events-none absolute inset-0 flex items-center justify-end ${editing ? 'invisible' : ''}`}>
                <NumberFlow value={Number(part) || 0} className="text-ink-1" locales="zh-CN" format={FORMAT} trend={previous.current.trend}
                  spinTiming={SPIN} transformTiming={TRANSFORM} opacityTiming={OPACITY} respectMotionPreference />
              </span>
              <input aria-label={`${ariaLabel}：${label}`} aria-describedby={describedBy} aria-invalid={bad || undefined}
                inputMode="numeric" autoComplete="off" disabled={disabled} value={part}
                className={`block w-full border-0 bg-transparent p-0 text-right text-ink-1 outline-none ${editing ? '' : 'opacity-0'}`}
                onChange={event => edit(key, event.target.value)}
                onFocus={event => { setActive(key); setFocused(key); event.currentTarget.select() }} onKeyDown={keyDown} />
            </span>
            <span className="text-[13px] text-ink-3">{label}</span>
          </label>
        })}
      </div>
    </NumberFlowGroup>
    <button type="button" className={`${button} border-l border-line`} disabled={disabled || bad || increase === value}
      aria-label={`${ariaLabel}：增加 1 ${unit.label}`} title={`增加 1 ${unit.label}`} onClick={() => apply(increase)}>+</button>
    <span className="sr-only" aria-live="polite" aria-atomic="true">{focused === null && valid ? `${draft.parts.days}天${draft.parts.hours}小时` : ''}</span>
  </div>
}
