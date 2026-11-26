import { useId } from 'react'
import { LeverageInput } from '@/features/account/LeverageInput'
import { leverageError, type LeverageLimits } from '@/features/account/leverage'

export function SignalLeverageFields({ limits, value, onChange }: {
  limits: LeverageLimits
  value: { long: string; short: string }
  onChange: (value: { long: string; short: string }) => void
}) {
  const id = useId()
  return <div className="grid gap-2">
    <div className="flex flex-wrap gap-x-4 gap-y-2">
      {(['long', 'short'] as const).map((side) => {
        const inputId = `${id}-${side}`
        const error = leverageError(value[side], limits)
        return <div key={side} className="grid gap-1">
          <label htmlFor={inputId} className="text-[14px] text-ink-2">
            {side === 'long' ? '做多杠杆' : '做空杠杆'}
          </label>
          <LeverageInput id={inputId} limits={limits} value={value[side]}
            invalid={Boolean(error)} describedBy={error ? `${inputId}-error` : `${id}-help`}
            onChange={(next) => onChange({ ...value, [side]: next })} />
          {error && <p id={`${inputId}-error`} className="text-[13px] text-warn">{error}</p>}
        </div>
      })}
    </div>
    <p id={`${id}-help`} className="text-[13px] text-ink-3">供组合函数读取，目标数量由函数决定。</p>
  </div>
}
