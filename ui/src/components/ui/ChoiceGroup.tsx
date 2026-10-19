import { useId, useRef, type ReactElement } from 'react'
import { Check } from 'lucide-react'

export interface ChoiceOption<T extends string> {
  value: T
  label: string
  description?: string
  disabled?: boolean
}

/** 参数单选组：紧凑按钮或带说明卡片，共用 radio 键盘语义。 */
export function ChoiceGroup<T extends string>({
  value, options, onChange, className = '', ariaLabel, describedBy, disabled = false,
  invalid = false, variant = 'buttons',
}: {
  value: T
  options: ChoiceOption<T>[]
  onChange: (value: T) => void
  className?: string
  ariaLabel?: string
  describedBy?: string
  disabled?: boolean
  invalid?: boolean
  variant?: 'buttons' | 'cards'
}): ReactElement {
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const id = useId()
  const enabled = options.map((o, i) => !o.disabled ? i : -1).filter((i) => i >= 0)
  const selected = options.findIndex((o) => o.value === value && !o.disabled)
  const focusIndex = selected >= 0 ? selected : enabled[0]
  return (
    <div role="radiogroup" aria-label={ariaLabel} aria-describedby={describedBy}
      aria-disabled={disabled || undefined} aria-invalid={invalid || undefined}
      className={`${variant === 'cards' ? 'grid w-full gap-2 sm:grid-cols-3' : 'flex flex-wrap gap-2'} ${className}`}>
      {options.map((option, index) => (
        <button key={option.value} ref={(node) => { refs.current[index] = node }} type="button" role="radio"
          aria-checked={option.value === value} disabled={disabled || option.disabled}
          aria-describedby={option.description ? `${id}-${index}` : undefined}
          tabIndex={index === focusIndex ? 0 : -1}
          className={`min-w-0 cursor-pointer rounded-[9px] border px-3 py-2 text-left text-[15px] transition-colors duration-200 motion-reduce:transition-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-40 ${
            option.value === value ? 'border-accent bg-accent-soft text-ink-1' : 'border-ink-3/30 text-ink-2 hover:border-ink-3/60'
          }`}
          onClick={() => onChange(option.value)}
          onKeyDown={(event) => {
            const current = enabled.indexOf(index)
            let target: number | undefined
            if (event.key === 'Home') target = enabled[0]
            else if (event.key === 'End') target = enabled.at(-1)
            else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') target = enabled[(current + 1) % enabled.length]
            else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') target = enabled[(current - 1 + enabled.length) % enabled.length]
            else return
            event.preventDefault()
            if (target === undefined || disabled) return
            onChange(options[target]!.value)
            refs.current[target]?.focus()
          }}>
          <span className="flex items-center justify-between gap-2">
            <span>{option.label}</span>
            <Check size={13} aria-hidden="true" className={`shrink-0 ${option.value === value ? '' : 'invisible'}`} />
          </span>
          {option.description && <span id={`${id}-${index}`} className="mt-1 block text-xs leading-relaxed text-ink-3">{option.description}</span>}
        </button>
      ))}
    </div>
  )
}
