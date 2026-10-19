import NumberFlow from '@number-flow/react'
import { useRef, useState } from 'react'
import type { KeyboardEvent, Ref } from 'react'
import { stepNumericValue } from '@/components/ui/numericStepper'

const SPIN_TIMING = { duration: 220, easing: 'cubic-bezier(0.32, 0.72, 0, 1)' }
const TRANSFORM_TIMING = { duration: 180, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const OPACITY_TIMING = { duration: 160, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
const NUMBER_FORMAT = { useGrouping: false }

/** 尺寸档：`md` 用于表单页，`sm` 用于行内编辑等紧凑处。 */
type StepperSize = 'sm' | 'md'

const SIZE_CLASS: Record<StepperSize, { box: string; button: string; value: string; inputW: string; text: string; unit: string }> = {
  md: { box: 'h-10 rounded-[9px]', button: 'w-9 text-[19px]', value: 'min-w-[88px] px-2', inputW: 'w-12', text: 'text-[16px]', unit: 'text-[13px]' },
  sm: { box: 'h-7 rounded-[8px]', button: 'w-6 text-[15px]', value: 'px-1', inputW: 'w-[4ch]', text: 'text-[14px]', unit: 'text-[12px]' },
}

interface StepperNumberInputProps {
  /** 字符串草稿（输入态可能非法），校验与提交语义归使用方。 */
  value: string
  onChange: (value: string) => void
  /** 步长、区间全部由使用方决定；max 缺省 = 无上限。 */
  step: number
  min?: number
  max?: number
  exclusiveMin?: boolean
  exclusiveMax?: boolean
  appearance?: 'boxed' | 'plain'
  decimal?: boolean
  disabled?: boolean
  /** 单位文案（如 次 / 毫秒 / 秒），缺省不显示。 */
  unit?: string
  ariaLabel?: string
  /** 非法态：边框转琥珀；同时强制显示输入层（不用滚动数字遮盖非法草稿）。 */
  invalid?: boolean
  size?: StepperSize
  /** 自定义步进；缺省用 :func:`stepNumericValue`（±step 夹区间，非法草稿不动）。 */
  onStep?: (value: string, direction: -1 | 1) => string
  /** 提供且当前未聚焦、草稿合法时：以滚动数字覆盖展示该值（NumberFlow）。 */
  displayValue?: number
  /** Enter 键：提供则回调；否则草稿合法时 blur 收手。 */
  onEnter?: () => void
  /** 聚焦时全选草稿（行内编辑场景：直接打字覆盖）。 */
  selectOnFocus?: boolean
  id?: string
  describedBy?: string
  ref?: Ref<HTMLInputElement>
}

/**
 * 数字步进输入：文本框 + inputMode（不用原生 number input，规避原生步进箭头），
 * 两侧 − / + 步进，↑↓ 键同效。大小、步进、区间、单位全部参数化，组件不自带业务语义。
 *
 * 草稿是字符串：组件不拦截非法输入（用户可能正在打中间态），
 * 非法草稿下步进按钮自动禁用（步进结果与草稿相同视为不可步进）。
 */
export function StepperNumberInput({
  value,
  onChange,
  step,
  min,
  max,
  exclusiveMin = false,
  exclusiveMax = false,
  appearance = 'boxed',
  decimal = false,
  disabled = false,
  unit,
  ariaLabel,
  invalid = false,
  size = 'md',
  onStep,
  displayValue,
  onEnter,
  selectOnFocus = false,
  id,
  describedBy,
  ref,
}: StepperNumberInputProps) {
  const sz = SIZE_CLASS[size]
  const plain = appearance === 'plain'
  const autoWidth = plain ? { width: `${Math.max(1, Math.min(18, value.length))}ch` } : undefined
  const inputWidth = plain ? '' : decimal ? 'w-[10ch]' : sz.inputW
  const innerRef = useRef<HTMLInputElement>(null)
  const [focused, setFocused] = useState(false)

  const parsed = Number(value)
  const valid = value.trim() !== '' && Number.isFinite(parsed) && (decimal || Number.isInteger(parsed)) && (min === undefined || (exclusiveMin ? parsed > min : parsed >= min)) && (max === undefined || (exclusiveMax ? parsed < max : parsed <= max))
  const stepFn = onStep ?? ((v: string, direction: -1 | 1) => stepNumericValue(v, direction, { step, min, max, decimal, exclusiveMin, exclusiveMax }))
  const decreaseValue = stepFn(value, -1)
  const increaseValue = stepFn(value, 1)
  const decreaseDisabled = decreaseValue === value
  const increaseDisabled = increaseValue === value

  // 显示层（滚动数字）只在提供了展示值、未聚焦且草稿合法时出现；其余时刻输入层恒显
  const showInput = displayValue === undefined || focused || invalid || !valid

  const applyStep = (next: string) => {
    if (next === value) return
    setFocused(false)
    onChange(next)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (disabled) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      applyStep(event.key === 'ArrowUp' ? increaseValue : decreaseValue)
      return
    }
    if (event.key === 'Enter') {
      if (onEnter) {
        event.preventDefault()
        onEnter()
      } else if (valid) {
        event.preventDefault()
        event.currentTarget.blur()
      }
    }
  }

  const setRefs = (el: HTMLInputElement | null) => {
    innerRef.current = el
    if (typeof ref === 'function') ref(el)
    else if (ref) ref.current = el
  }

  const stepButton =
    'grid h-full flex-none cursor-pointer place-items-center border-0 bg-transparent text-ink-3 transition-colors hover:bg-fill hover:text-ink-1 disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-ink-3'
  const plainButton = 'grid h-9 w-9 shrink-0 cursor-pointer place-items-center rounded-full border border-line bg-transparent text-xl text-ink-3 transition-colors hover:border-accent hover:text-accent focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-30 motion-reduce:transition-none'
  const unitText = unit ? <span className={`flex-none text-ink-3 ${sz.unit}`}>{unit}</span> : null

  return (
    <div
      className={plain ? `inline-flex items-center gap-2 ${disabled ? 'opacity-40' : ''}` : `flex overflow-hidden border bg-surface ${sz.box} ${
        invalid ? 'border-warn focus-within:border-warn' : 'border-ink-3/30 contrast:border-border-strong focus-within:border-accent'
      }`}
    >
      <button
        type="button"
        aria-label={`减少 ${step}${unit ? ` ${unit}` : ''}`}
        title={`减少 ${step}${unit ? ` ${unit}` : ''}`}
        className={plain ? plainButton : `${stepButton} ${sz.button} border-r border-r-line`}
        disabled={disabled || decreaseDisabled}
        onClick={() => applyStep(decreaseValue)}
      >
        −
      </button>
      <div className={`relative cursor-text ${plain ? 'h-10 text-2xl' : sz.value}`}>
        {displayValue !== undefined && (
          <div
            aria-hidden="true"
            className={`pointer-events-none absolute inset-0 flex items-center justify-center gap-1 ${
              showInput ? 'opacity-0' : 'opacity-100'
            }`}
          >
            {/* 与输入层同宽同对齐（定宽右对齐），切换时数字位置零位移，只滚不走 */}
            <span style={autoWidth} className={`flex justify-end ${inputWidth}`}>
              <NumberFlow
                value={displayValue}
                locales="zh-CN"
                format={decimal ? { ...NUMBER_FORMAT, maximumFractionDigits: 15 } : NUMBER_FORMAT}
                className={`num text-ink-1 ${plain ? 'text-2xl' : sz.text}`}
                spinTiming={plain ? undefined : SPIN_TIMING}
                transformTiming={plain ? undefined : TRANSFORM_TIMING}
                opacityTiming={plain ? undefined : OPACITY_TIMING}
                respectMotionPreference
              />
            </span>
            {unitText}
          </div>
        )}
        <div
          className={`relative flex h-full items-center justify-center gap-1 ${showInput ? 'opacity-100' : 'opacity-0'}`}
          onMouseDown={(event) => {
            if (event.target === innerRef.current) return
            event.preventDefault()
            innerRef.current?.focus()
          }}
        >
          <input
            ref={setRefs}
            style={autoWidth}
            id={id}
            aria-label={ariaLabel}
            aria-describedby={describedBy}
            aria-invalid={invalid || undefined}
            inputMode={decimal ? "decimal" : "numeric"}
            disabled={disabled}
            className={`num min-w-0 border-0 bg-transparent p-0 text-right text-ink-1 outline-none ${inputWidth} ${plain ? 'text-2xl' : sz.text}`}
            value={value}
            onChange={(event) => { setFocused(true); onChange(event.target.value) }}
            onFocus={(event) => {
              setFocused(true)
              if (selectOnFocus) event.currentTarget.select()
            }}
            onBlur={() => setFocused(false)}
            onKeyDown={onKeyDown}
          />
          {unitText}
        </div>
      </div>
      <button
        type="button"
        aria-label={`增加 ${step}${unit ? ` ${unit}` : ''}`}
        title={`增加 ${step}${unit ? ` ${unit}` : ''}`}
        className={plain ? plainButton : `${stepButton} ${sz.button} border-l border-l-line`}
        disabled={disabled || increaseDisabled}
        onClick={() => applyStep(increaseValue)}
      >
        +
      </button>
    </div>
  )
}
