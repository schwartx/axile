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
  invalid?: boolean
  describedBy?: string
  inlineHint?: boolean
}

/** 金额草稿保持原样，量级随输入更新。 */
export function AmountInput({ value, onChange, unit, ariaLabel, autoFocus, required, disabled, invalid, describedBy, inlineHint }: AmountInputProps) {
  const hintId = useId()
  const input = useRef<HTMLInputElement>(null)
  const hint = amountMagnitude(value)
  useEffect(() => {
    input.current?.setCustomValidity(value && !hint ? '请输入有效的非负金额' : '')
  }, [value, hint])

  const inputField = <input ref={input} type="text" inputMode="decimal" pattern={AMOUNT_PATTERN} autoComplete="off" spellCheck={false}
    aria-label={ariaLabel} aria-invalid={invalid || undefined} aria-describedby={[hintId, describedBy].filter(Boolean).join(' ')} autoFocus={autoFocus} required={required} disabled={disabled}
    className={`num min-w-0 py-2 text-sm text-ink-1 ${inlineHint
      ? 'flex-1 bg-transparent outline-none'
      : 'w-full rounded-md border border-line bg-surface px-3 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent disabled:opacity-45 aria-invalid:border-warn'}`}
    value={value} onChange={event => onChange(event.target.value)} />
  const hintText = hint ? `${hint}${unit}` : ''
  const hintLabel = <span id={hintId} role="status" aria-label="金额量级" title={inlineHint && hintText ? hintText : undefined}
    className={inlineHint
      ? 'min-w-0 max-w-[40%] truncate text-right text-xs text-ink-3 not-empty:ml-2'
      : 'min-h-4 pl-3 text-xs leading-4 text-ink-3 [overflow-wrap:anywhere]'}>
    {hintText}
  </span>

  return <span className="grid min-w-0 gap-1">
    <span className="flex min-w-0 items-center gap-2">
      {inlineHint ? <span className={`flex min-w-0 flex-1 items-center rounded-md border bg-surface px-3 focus-within:outline-2 focus-within:-outline-offset-2 focus-within:outline-accent has-disabled:opacity-45 ${invalid ? 'border-warn' : 'border-line'}`}>
        {inputField}
        {hintLabel}
      </span> : inputField}
      <span aria-hidden="true" className="shrink-0 text-xs text-ink-3">{unit}</span>
    </span>
    {!inlineHint && hintLabel}
  </span>
}
