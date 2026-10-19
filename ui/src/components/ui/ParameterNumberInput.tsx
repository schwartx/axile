import { useId } from 'react'
import { ValueSlider } from '@/components/ui/ValueSlider'
import { StepperNumberInput } from '@/components/ui/StepperNumberInput'

export interface ParameterNumberInputProps {
  value: string
  onChange: (value: string) => void
  label: string
  unit?: string
  step: number
  min?: number
  max?: number
  exclusiveMin?: boolean
  exclusiveMax?: boolean
  decimal?: boolean
  disabled?: boolean
  error?: string
  describedBy?: string
  mode?: 'stepper' | 'numberflow' | 'presets' | 'slider'
  presets?: number[]
  sliderMin?: number
  sliderMax?: number
}

/** 所有入口共享字符串草稿；滑块仅在主动操作时将值吸附到步长。 */
export function ParameterNumberInput({
  value, onChange, label, unit, step, min, max, exclusiveMin, exclusiveMax, decimal = false, disabled = false,
  error, describedBy, mode = 'stepper', presets = [], sliderMin, sliderMax,
}: ParameterNumberInputProps) {
  const errorId = useId()
  const parsed = Number(value)
  const lower = sliderMin ?? min
  const upper = sliderMax ?? max
  const hasRange = lower !== undefined && upper !== undefined && Number.isFinite(lower) && Number.isFinite(upper) && lower < upper
  const finite = value.trim() !== '' && Number.isFinite(parsed)
  const description = [describedBy, error ? errorId : undefined].filter(Boolean).join(' ') || undefined
  return (
    <div className="flex w-full max-w-80 flex-col gap-2">
      {mode === 'slider' && hasRange && (
        <ValueSlider label={`${label}滑块`} describedBy={description} value={finite ? parsed : Number.NaN}
          min={lower} max={upper} step={step} disabled={disabled} invalid={!!error} unit={unit}
          onChange={(next) => onChange(String(next))} />
      )}
      <div className="flex justify-end">
        {mode === 'stepper' || mode === 'numberflow' ? (
          <StepperNumberInput value={value} onChange={onChange} step={step} min={min} max={max}
            appearance={mode === 'numberflow' ? 'plain' : 'boxed'} exclusiveMin={exclusiveMin} exclusiveMax={exclusiveMax}
            displayValue={finite ? parsed : undefined} decimal={decimal} disabled={disabled} invalid={!!error} unit={unit} ariaLabel={label} describedBy={description} />
        ) : (
          <div className={`flex items-center gap-2 rounded-[9px] border bg-surface px-3 py-2 ${error ? 'border-warn focus-within:border-warn' : 'border-ink-3/30 contrast:border-border-strong focus-within:border-accent'}`}>
            <input aria-label={label} aria-describedby={description} aria-invalid={!!error || undefined}
              inputMode={decimal ? 'decimal' : 'numeric'} disabled={disabled} value={value}
              className="num w-28 min-w-0 bg-transparent text-right text-[15px] outline-none disabled:opacity-40"
              onChange={(event) => onChange(event.target.value)} />
            {unit && <span className="text-xs text-ink-3">{unit}</span>}
          </div>
        )}
      </div>
      {mode === 'presets' && <div className="flex flex-wrap justify-end gap-2">
        {presets.map((preset) => <button key={preset} type="button" disabled={disabled}
          aria-pressed={finite && parsed === preset}
          className={`rounded-md border px-2 py-1 text-xs disabled:opacity-40 ${finite && parsed === preset ? 'border-accent bg-accent-soft' : 'border-line text-ink-2'}`}
          onClick={() => onChange(String(preset))}>{preset}{unit}</button>)}
      </div>}
      {error && <span id={errorId} role="alert" className="text-xs text-warn">{error}</span>}
    </div>
  )
}
