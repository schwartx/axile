import NumberFlow, { NumberFlowGroup } from '@number-flow/react'
import { useRef, useState, type KeyboardEvent } from 'react'
import { joinDuration, splitDuration, formatDuration, durationPartVisible, type DurationParts } from '@/components/ui/duration'
import { stepNumericValue } from '@/components/ui/numericStepper'

interface DurationInputProps {
  value: string
  onChange: (value: string) => void
  step: number
  min?: number
  max?: number
  exclusiveMin?: boolean
  exclusiveMax?: boolean
  disabled?: boolean
  invalid?: boolean
  size?: 'sm' | 'md'
  id?: string
  ariaLabel: string
  describedBy?: string
}

const UNITS = [
  { key: 'hours', label: '时' }, { key: 'minutes', label: '分' }, { key: 'seconds', label: '秒' },
] as const
const FORMAT = { useGrouping: false }
const SIXTY_DIGITS = { 1: { max: 5 } }
const SPIN = { duration: 220, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' }
const TRANSFORM = { duration: 180, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const OPACITY = { duration: 160, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }

/** 整数秒时长：真实输入层保留草稿与焦点，展示层按总时长方向联动进位。 */
export function DurationInput({ value, onChange, step, min, max, exclusiveMin, exclusiveMax,
  disabled = false, invalid = false, size = 'md', id, ariaLabel, describedBy }: DurationInputProps) {
  // 范围决定可编辑分段；零单位只收起，不卸载输入框。
  const showHours = max === undefined || max >= 3600
  const units = showHours ? UNITS : UNITS.slice(1)
  const [draft, setDraft] = useState(() => ({ source: value, parts: splitDuration(value, showHours) }))
  const [focused, setFocused] = useState<keyof DurationParts | null>(null)
  const visible = (key: keyof DurationParts) => durationPartVisible(draft.parts, key, focused !== null)
  const firstVisible = units.find(({ key }) => visible(key))?.key
  if (draft.source !== value) setDraft({ source: value, parts: splitDuration(value, showHours) })
  const total = Number(value)
  const previous = useRef({ value: total, trend: 1 })
  if (Number.isSafeInteger(total) && total >= 0 && total !== previous.current.value)
    previous.current = { value: total, trend: total < previous.current.value ? -1 : 1 }
  const valid = value.trim() !== '' && Number.isSafeInteger(total) && total >= 0
  const bad = invalid || !valid || (min !== undefined && (exclusiveMin ? total <= min : total < min))
    || (max !== undefined && (exclusiveMax ? total >= max : total > max))
  const options = { step, min, max, exclusiveMin, exclusiveMax }
  const decrease = stepNumericValue(value, -1, options)
  const increase = stepNumericValue(value, 1, options)
  const small = size === 'sm'
  const button = `grid h-full shrink-0 cursor-pointer place-items-center bg-transparent text-ink-3 transition-colors hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-30 motion-reduce:transition-none ${small ? 'w-6 text-[15px]' : 'w-9 text-[19px]'}`
  const apply = (next: string) => {
    if (disabled || next === value) return
    setDraft({ source: next, parts: splitDuration(next, showHours) })
    onChange(next)
  }
  const edit = (key: keyof DurationParts, next: string) => {
    const parts = { ...draft.parts, [key]: next }
    const source = joinDuration(parts)
    setDraft({ source, parts })
    onChange(source)
  }
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault()
      apply(event.key === 'ArrowUp' ? increase : decrease)
    } else if (event.key === 'Enter' && !bad) {
      event.preventDefault()
      setDraft({ source: value, parts: splitDuration(value, showHours) })
      event.currentTarget.blur()
    }
  }

  return <div role="group" aria-label={`${ariaLabel}输入`} data-duration-input
    onBlur={event => {
      if (event.relatedTarget instanceof HTMLInputElement && event.currentTarget.contains(event.relatedTarget)) return
      setFocused(null)
      if (valid && !event.currentTarget.contains(event.relatedTarget))
        setDraft({ source: value, parts: splitDuration(value, showHours) })
    }}
    className={`inline-flex shrink-0 items-center overflow-hidden border bg-surface ${small ? 'h-7 rounded-[8px]' : 'h-10 rounded-[9px]'} ${disabled ? 'opacity-40' : ''} ${bad ? 'border-warn focus-within:border-warn' : 'border-ink-3/30 contrast:border-border-strong focus-within:border-accent'}`}>
    <button type="button" className={`${button} border-r border-line`} disabled={disabled || bad || decrease === value}
      aria-label={`${ariaLabel}：减少 ${formatDuration(step)}`} title={`减少 ${formatDuration(step)}`}
      onClick={() => { setFocused(null); apply(decrease) }}>−</button>
    <NumberFlowGroup>
      <div className={`flex h-full items-center px-1 ${small ? 'text-[14px]' : 'text-[16px]'}`}>
        {units.map(({ key, label }) => {
          const part = draft.parts[key]
          const editing = focused === key || !/^\d+$/.test(part)
          const shown = visible(key)
          return <span key={key} data-duration-unit={key} inert={!shown} aria-hidden={!shown}
            className={`grid transition-[grid-template-columns] duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${shown ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]'}`}>
            <span className="min-w-0 overflow-hidden">
              <label className={`flex items-center gap-0.5 cursor-text ${small ? 'px-0.5' : 'px-1'}`}>
                <span className="relative block num" style={{ width: `${Math.max(2, Math.min(16, part.length))}ch` }}>
                  <span aria-hidden className={`pointer-events-none absolute inset-0 flex items-center justify-end ${editing ? 'invisible' : ''}`}>
                    <NumberFlow value={Number(part) || 0} locales="zh-CN" format={FORMAT}
                      trend={previous.current.trend} digits={Number(part) < 60 && key !== 'hours' && (key === 'seconds' || showHours) ? SIXTY_DIGITS : undefined}
                      spinTiming={SPIN} transformTiming={TRANSFORM} opacityTiming={OPACITY} respectMotionPreference />
                  </span>
                  <input id={key === firstVisible ? id : undefined} aria-label={`${ariaLabel}：${label}`}
                    aria-describedby={describedBy} aria-invalid={bad || undefined} inputMode="numeric" disabled={disabled}
                    className={`block w-full border-0 bg-transparent p-0 text-right text-ink-1 outline-none ${editing ? '' : 'opacity-0'}`}
                    value={part} onChange={event => edit(key, event.target.value)}
                    onFocus={event => { setFocused(key); event.currentTarget.select() }} onKeyDown={onKeyDown} />
                </span>
                <span className={`text-ink-3 ${small ? 'text-[12px]' : 'text-[13px]'}`}>{label}</span>
              </label>
            </span>
          </span>
        })}
      </div>
    </NumberFlowGroup>
    <button type="button" className={`${button} border-l border-line`} disabled={disabled || bad || increase === value}
      aria-label={`${ariaLabel}：增加 ${formatDuration(step)}`} title={`增加 ${formatDuration(step)}`}
      onClick={() => { setFocused(null); apply(increase) }}>+</button>
  </div>
}
