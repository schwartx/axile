import { useRef, useState } from 'react'
import { sliderValueAt } from '@/components/ui/sliderValue'

/** 自绘单值滑条：44px 操作区、指针捕获、刻度和完整键盘语义。 */
export function ValueSlider({ value, min, max, step, onChange, onChangeEnd, formatValue, label, unit = '', disabled = false, describedBy, invalid = false, variant = 'default' }: {
  value: number
  min: number
  max: number
  step: number
  onChange: (value: number) => void
  onChangeEnd?: (value: number) => void
  formatValue?: (value: number) => string
  label: string
  unit?: string
  disabled?: boolean
  describedBy?: string
  invalid?: boolean
  variant?: 'default' | 'axis'
}) {
  const track = useRef<HTMLDivElement>(null)
  const pointer = useRef<number | null>(null)
  const pending = useRef<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const bounded = Math.max(min, Math.min(max, Number.isFinite(value) ? value : min))
  const fraction = (bounded - min) / (max - min)
  const format = formatValue ?? ((next: number) => `${next}${unit}`)
  // 用易读的参考数值标刻度，位置仍按真实值计算，不改变参数精度。
  const marks = [...new Set([0.25, 0.5, 0.75].map((ratio) => Number((min + ratio * (max - min)).toPrecision(2))))]
    .filter((mark) => mark > min && mark < max)
  const change = (next: number) => { pending.current = next; onChange(next) }
  const commit = () => {
    const next = pending.current
    pending.current = null
    if (next != null) onChangeEnd?.(next)
  }
  const finishPointer = () => {
    pointer.current = null
    setDragging(false)
    commit()
  }
  const changeAt = (clientX: number) => {
    const rect = track.current?.getBoundingClientRect()
    if (!rect || rect.width === 0) return
    change(sliderValueAt((clientX - rect.left) / rect.width, min, max, step))
  }
  return <div className={`w-full ${disabled ? 'opacity-40' : ''}`}>
    <div role="slider" aria-label={label} aria-describedby={describedBy} aria-orientation="horizontal"
      aria-valuemin={min} aria-valuemax={max} aria-valuenow={bounded}
      aria-valuetext={Number.isFinite(value) ? format(value) : '请输入数值'}
      aria-disabled={disabled || undefined} aria-invalid={invalid || undefined} tabIndex={disabled ? -1 : 0}
      className={`group relative flex ${variant === 'axis' ? 'h-6 rounded' : 'h-11 rounded-lg focus-visible:ring-2 focus-visible:ring-accent/40'} touch-pan-y select-none items-center px-3 outline-none ${disabled ? 'cursor-default' : dragging ? 'cursor-grabbing' : 'cursor-pointer'}`}
      onPointerDown={(event) => {
        if (disabled || !event.isPrimary || event.button !== 0) return
        event.currentTarget.focus()
        event.currentTarget.setPointerCapture(event.pointerId)
        pointer.current = event.pointerId
        setDragging(true)
        changeAt(event.clientX)
      }}
      onPointerMove={(event) => {
        if (!disabled && pointer.current === event.pointerId) changeAt(event.clientX)
      }}
      onPointerUp={(event) => {
        if (pointer.current !== event.pointerId) return
        if (!disabled) changeAt(event.clientX)
        finishPointer()
        event.currentTarget.releasePointerCapture(event.pointerId)
      }}
      onLostPointerCapture={finishPointer}
      onPointerCancel={finishPointer}
      onBlur={finishPointer}
      onKeyDown={(event) => {
        if (disabled) return
        let next: number
        if (event.key === 'Home') next = min
        else if (event.key === 'End') next = max
        else if (['ArrowLeft', 'ArrowDown', 'ArrowRight', 'ArrowUp', 'PageUp', 'PageDown'].includes(event.key)) {
          const direction = ['ArrowLeft', 'ArrowDown', 'PageDown'].includes(event.key) ? -1 : 1
          const delta = step * (event.key.startsWith('Page') || event.shiftKey ? 10 : 1)
          next = Number(Math.max(min, Math.min(max, (pending.current ?? bounded) + direction * delta)).toPrecision(15))
        } else return
        event.preventDefault()
        change(next)
      }}
      onKeyUp={(event) => {
        if (['Home', 'End', 'ArrowLeft', 'ArrowDown', 'ArrowRight', 'ArrowUp', 'PageUp', 'PageDown'].includes(event.key)) commit()
      }}>
      <div ref={track} className={`relative w-full ${variant === 'axis' ? 'h-px bg-transparent' : 'h-1.5 rounded-full bg-ink-3/15'}`}>
        {variant !== 'axis' && <div className={`absolute inset-y-0 left-0 rounded-full ${invalid ? 'bg-warn' : 'bg-accent'}`} style={{ width: `${fraction * 100}%` }} />}
        {variant !== 'axis' && marks.map((mark) => <span key={mark} aria-hidden="true"
          className={`absolute top-1/2 h-1 w-1 -translate-x-1/2 -translate-y-1/2 rounded-full ${mark <= bounded ? 'bg-surface/70' : 'bg-ink-3/40'}`}
          style={{ left: `${(mark - min) / (max - min) * 100}%` }} />)}
        {variant === 'axis' ? <span data-slider-thumb aria-hidden="true" style={{ left: `${fraction * 100}%` }} className={`absolute top-1/2 h-0 w-0 -translate-x-1/2 -translate-y-1/2 border-x-4 border-b-[6px] border-x-transparent ${invalid ? 'border-b-warn' : 'border-b-accent'}`} /> : <span data-slider-thumb aria-hidden="true" style={{ left: `${fraction * 100}%` }}
          className={`absolute top-1/2 flex h-6 w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center gap-0.5 rounded-full border-2 bg-surface shadow-sm transition-shadow duration-150 motion-reduce:transition-none ${invalid ? 'border-warn' : 'border-accent'} ${dragging ? 'ring-4 ring-accent/15' : disabled ? '' : 'group-hover:ring-4 group-hover:ring-accent/10'}`}>
          <span className="h-2 w-px rounded-full bg-ink-3/60" /><span className="h-2 w-px rounded-full bg-ink-3/60" />
        </span>}
      </div>
    </div>
    {variant !== 'axis' && <div aria-hidden="true" className="num flex justify-between px-3 text-[11px] text-ink-3">
      <span>{format(min)}</span><span>{marks.length === 3 ? format(marks[1]) : ''}</span><span>{format(max)}</span>
    </div>}
  </div>
}
