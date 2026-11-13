import { useEffect, useId, useRef } from 'react'
import { AMOUNT_PATTERN, amountMagnitude } from '@/components/ui/amountMagnitude'

interface AmountInputProps {
  value: string
  onChange: (value: string) => void
  unit: string
  ariaLabel: string
  autoFocus?: boolean
  required?: boolean
  disabled?: boolean
}

/** 金额草稿保持原样；下方独立提示量级，输入时不改写数值或移动光标。 */
export function AmountInput({ value, onChange, unit, ariaLabel, autoFocus, required, disabled }: AmountInputProps) {
  const hintId = useId()
  const input = useRef<HTMLInputElement>(null)
  const hint = amountMagnitude(value)
  useEffect(() => {
    input.current?.setCustomValidity(value && !hint ? '请输入有效的非负金额' : '')
  }, [value, hint])

  return <span className="grid min-w-0 gap-1">
    <span className="flex min-w-0 items-center gap-2">
      <input ref={input} type="text" inputMode="decimal" pattern={AMOUNT_PATTERN} autoComplete="off" spellCheck={false}
        aria-label={ariaLabel} aria-describedby={hintId} autoFocus={autoFocus} required={required} disabled={disabled}
        className="num min-w-0 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm text-ink-1 focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-45"
        value={value} onChange={event => onChange(event.target.value)} />
      <span aria-hidden="true" className="shrink-0 text-xs text-ink-3">{unit}</span>
    </span>
    <span id={hintId} role="status" aria-label="金额量级" className="min-h-4 text-xs leading-4 text-ink-3 [overflow-wrap:anywhere]">
      {hint ? `${hint}${unit}` : ''}
    </span>
  </span>
}
