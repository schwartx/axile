import NumberFlow from '@number-flow/react'
import { useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

import {
  stepWeightPrecision,
  weightPrecisionError,
  weightPrecisionPercent,
} from '@/features/account/weightPrecision'

const SPIN_TIMING = { duration: 220, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' }
const TRANSFORM_TIMING = { duration: 180, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const OPACITY_TIMING = { duration: 160, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const NUMBER_FORMAT = { useGrouping: false, maximumFractionDigits: 20 }

interface WeightPrecisionInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  invalid?: boolean
  error?: string | null
}

/** 以十倍数量级调整、同时展示百分比含义的权重精度控件。 */
export function WeightPrecisionInput({
  id,
  value,
  onChange,
  invalid = false,
  error,
}: WeightPrecisionInputProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [isEditing, setIsEditing] = useState(false)
  const validValue = weightPrecisionError(value) === null ? Number(value) : null
  const lastValidValue = useRef(validValue ?? 0.01)
  if (validValue !== null) lastValidValue.current = validValue

  const showInput = isEditing || invalid || validValue === null
  const decreaseValue = stepWeightPrecision(value, -1)
  const increaseValue = stepWeightPrecision(value, 1)
  const percent = weightPrecisionPercent(value)
  const helpId = id ? `${id}-help` : undefined

  const applyStep = (next: string) => {
    if (next === value) return
    setIsEditing(false)
    onChange(next)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' && validValue !== null) {
      event.preventDefault()
      event.currentTarget.blur()
      return
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    const next = event.key === 'ArrowUp' ? increaseValue : decreaseValue
    if (next !== value) onChange(next)
  }

  const stepButton =
    'grid h-full w-9 flex-none place-items-center border-0 bg-transparent text-[19px] text-ink-3 transition-colors hover:bg-fill hover:text-ink-1 disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-ink-3'

  return (
    <div>
      <div
        className={`flex h-10 overflow-hidden rounded-[9px] border bg-surface ${
          invalid ? 'border-warn focus-within:border-warn' : 'border-ink-3/30 contrast:border-border-strong focus-within:border-ink-2'
        }`}
      >
        <button
          type="button"
          aria-label="提高权重精度十倍"
          title="提高精度十倍"
          className={`${stepButton} border-r border-r-line`}
          disabled={decreaseValue === value}
          onClick={() => applyStep(decreaseValue)}
        >
          −
        </button>
        <div className="relative min-w-[78px] cursor-text px-2">
          <div
            aria-hidden="true"
            className={`pointer-events-none absolute inset-0 flex items-center justify-center ${
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
          </div>
          <div
            className={`relative flex h-full items-center justify-center ${showInput ? 'opacity-100' : 'opacity-0'}`}
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
              aria-describedby={helpId}
              aria-invalid={invalid}
              inputMode="decimal"
              className="num w-[70px] border-0 bg-transparent p-0 text-center text-[16px] text-ink-1 outline-none"
              value={value}
              onChange={(event) => onChange(event.target.value)}
              onFocus={() => setIsEditing(true)}
              onBlur={() => {
                if (validValue !== null) setIsEditing(false)
              }}
              onKeyDown={onKeyDown}
            />
          </div>
        </div>
        <button
          type="button"
          aria-label="降低权重精度十倍"
          title="降低精度十倍"
          className={`${stepButton} border-l border-l-line`}
          disabled={increaseValue === value}
          onClick={() => applyStep(increaseValue)}
        >
          +
        </button>
      </div>
      <div id={helpId} className={`mt-1 text-[12px] ${invalid ? 'text-warn' : 'text-ink-3'}`}>
        {error ?? (percent ? `权重最小变化 ${percent}` : '按十倍数量级设置')}
      </div>
    </div>
  )
}
