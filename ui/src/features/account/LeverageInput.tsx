import NumberFlow from '@number-flow/react'
import { useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

import { formatLeverage, leverageValue, stepLeverage, type LeverageLimits } from '@/features/account/leverage'

const SPIN_TIMING = { duration: 220, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' }
const TRANSFORM_TIMING = { duration: 180, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const OPACITY_TIMING = { duration: 160, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const NUMBER_FORMAT = {
  useGrouping: false,
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
}
const FAST_STEP = 1

interface LeverageInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  describedBy?: string
  invalid?: boolean
  limits?: LeverageLimits
}

/** 可直接输入、亦可按渠道步进或 1 倍快步进调整的账户杠杆控件。 */
export function LeverageInput({
  id,
  value,
  onChange,
  describedBy,
  invalid = false,
  limits,
}: LeverageInputProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [isEditing, setIsEditing] = useState(false)
  const validValue = leverageValue(value, limits)
  const lastValidValue = useRef(validValue ?? 0)
  if (validValue !== null) lastValidValue.current = validValue

  const showInput = isEditing || invalid || validValue === null
  const baseStep = limits?.step ?? 0.1
  const decreaseValue = stepLeverage(value, -1, limits)
  const increaseValue = stepLeverage(value, 1, limits)
  const fastDecreaseValue = stepLeverage(value, -1, limits, FAST_STEP)
  const fastIncreaseValue = stepLeverage(value, 1, limits, FAST_STEP)
  const decreaseDisabled = validValue === null || validValue <= (limits?.min ?? 0)
  const increaseDisabled = validValue === null || (limits !== undefined && validValue >= limits.max)

  const finishEditing = () => {
    if (validValue === null) return
    const formatted = formatLeverage(validValue, limits?.step)
    if (formatted !== value) onChange(formatted)
    setIsEditing(false)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' && validValue !== null) {
      event.preventDefault()
      event.currentTarget.blur()
      return
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    const next = event.shiftKey
      ? event.key === 'ArrowUp'
        ? fastIncreaseValue
        : fastDecreaseValue
      : event.key === 'ArrowUp'
        ? increaseValue
        : decreaseValue
    if (next !== value) onChange(next)
  }

  const applyStep = (next: string) => {
    if (next === value) return
    setIsEditing(false)
    onChange(next)
  }

  const stepButtonBase =
    'grid h-full w-9 flex-none place-items-center border-0 bg-transparent text-ink-3 transition-colors hover:bg-fill hover:text-ink-1 disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-ink-3'
  const stepButton = `${stepButtonBase} text-[19px]`
  const fastStepButton = `${stepButtonBase} text-[13px]`

  return (
    <div
      className={`flex h-10 overflow-hidden rounded-[9px] border bg-surface ${
        invalid ? 'border-warn focus-within:border-warn' : 'border-ink-3/30 contrast:border-border-strong focus-within:border-ink-2'
      }`}
    >
      <button
        type="button"
        aria-label="减少 1 倍杠杆"
        title="减少 1"
        className={`${fastStepButton} border-r border-r-line`}
        disabled={decreaseDisabled}
        onClick={() => applyStep(fastDecreaseValue)}
      >
        −1
      </button>
      <button
        type="button"
        aria-label={`减少 ${baseStep} 倍杠杆`}
        title={`减少 ${baseStep}`}
        className={`${stepButton} border-r border-r-line`}
        disabled={decreaseDisabled}
        onClick={() => applyStep(decreaseValue)}
      >
        −
      </button>
      <div className="relative min-w-[78px] cursor-text px-2">
        <div
          aria-hidden="true"
          className={`pointer-events-none absolute inset-0 flex items-center justify-center gap-1 ${
            showInput ? 'opacity-0' : 'opacity-100'
          }`}
        >
          <NumberFlow
            value={lastValidValue.current}
            locales="zh-CN"
            format={NUMBER_FORMAT}
            className="num text-[16px] text-ink-1"
            spinTiming={SPIN_TIMING}
            transformTiming={TRANSFORM_TIMING}
            opacityTiming={OPACITY_TIMING}
            respectMotionPreference
          />
          <span className="flex-none text-[13px] text-ink-3">×</span>
        </div>
        <div
          className={`relative flex h-full items-center justify-center gap-1 ${
            showInput ? 'opacity-100' : 'opacity-0'
          }`}
          onMouseDown={(event) => {
            if (event.target === inputRef.current) return
            event.preventDefault()
            inputRef.current?.focus()
          }}
        >
          <input
            ref={inputRef}
            id={id}
            role="spinbutton"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            aria-valuemin={limits?.min ?? 0}
            aria-valuemax={limits?.max}
            aria-valuenow={validValue ?? undefined}
            inputMode="decimal"
            className="num w-11 border-0 bg-transparent p-0 text-right text-[16px] text-ink-1 outline-none"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onFocus={() => setIsEditing(true)}
            onBlur={finishEditing}
            onKeyDown={onKeyDown}
          />
          <span className="flex-none text-[13px] text-ink-3">×</span>
        </div>
      </div>
      <button
        type="button"
        aria-label={`增加 ${baseStep} 倍杠杆`}
        title={`增加 ${baseStep}`}
        className={`${stepButton} border-l border-l-line`}
        disabled={increaseDisabled}
        onClick={() => applyStep(increaseValue)}
      >
        +
      </button>
      <button
        type="button"
        aria-label="增加 1 倍杠杆"
        title="增加 1"
        className={`${fastStepButton} border-l border-l-line`}
        disabled={increaseDisabled}
        onClick={() => applyStep(fastIncreaseValue)}
      >
        +1
      </button>
    </div>
  )
}
