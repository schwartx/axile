import { useRef, useState, type HTMLAttributes } from 'react'

type Props = Omit<HTMLAttributes<HTMLDivElement>, 'onChange' | 'onReset'> & {
  orientation: 'horizontal' | 'vertical'
  value: number
  min: number
  max: number
  step: number
  disabled?: boolean
  valueAt: (position: number) => number
  keyboardValue?: () => number
  onChange: (value: number) => void
  onCommit: (value: number) => void
  onReset: () => void
  onResizeChange?: (resizing: boolean) => void
}

/** 只共享分割线交互；坐标换算、布局和持久化由调用方负责。 */
export function ResizeHandle({ orientation, value, min, max, step, disabled = false, valueAt, keyboardValue,
  onChange, onCommit, onReset, onResizeChange, className = '', ...props }: Props) {
  const pointer = useRef<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const horizontal = orientation === 'horizontal'
  const stop = () => {
    if (pointer.current == null) return
    pointer.current = null
    setDragging(false)
    onResizeChange?.(false)
  }
  return <div {...props} role="separator" aria-orientation={orientation} aria-valuemin={min} aria-valuemax={max}
    aria-valuenow={value} aria-disabled={disabled || undefined} tabIndex={disabled ? -1 : 0} inert={disabled}
    className={`group touch-none outline-none ${horizontal ? 'relative z-10 cursor-row-resize' : 'cursor-col-resize'} ${className}`}
    data-dragging={dragging || undefined}
    onDoubleClick={() => { if (!disabled) onReset() }}
    onPointerDown={event => {
      if (disabled || !event.isPrimary || event.button !== 0 || pointer.current != null) return
      event.preventDefault()
      event.currentTarget.focus()
      event.currentTarget.setPointerCapture(event.pointerId)
      pointer.current = event.pointerId
      setDragging(true)
      onResizeChange?.(true)
      onChange(valueAt(horizontal ? event.clientY : event.clientX))
    }}
    onPointerMove={event => {
      if (pointer.current === event.pointerId) onChange(valueAt(horizontal ? event.clientY : event.clientX))
    }}
    onPointerUp={event => {
      if (pointer.current !== event.pointerId) return
      const next = valueAt(horizontal ? event.clientY : event.clientX)
      stop()
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
      onCommit(next)
    }}
    onPointerCancel={event => {
      if (pointer.current !== event.pointerId) return
      stop()
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    }}
    onLostPointerCapture={stop}
    onKeyDown={event => {
      if (disabled) return
      let next = keyboardValue?.() ?? value
      if (event.key === (horizontal ? 'ArrowUp' : 'ArrowLeft')) next -= step
      else if (event.key === (horizontal ? 'ArrowDown' : 'ArrowRight')) next += step
      else if (event.key === 'Home') next = min
      else if (event.key === 'End') next = max
      else return
      event.preventDefault()
      onCommit(Math.min(max, Math.max(min, next)))
    }}>
    <span aria-hidden className={`absolute ${horizontal ? 'inset-x-0 -inset-y-1' : 'inset-y-0 -inset-x-1'}`} />
    <span aria-hidden className={`pointer-events-none absolute transition-colors duration-130 motion-reduce:transition-none ${horizontal ? 'inset-x-0 top-1/2 h-px' : 'inset-y-0 left-1/2 w-px'} ${dragging ? 'bg-accent' : 'bg-line group-hover:bg-accent group-focus:bg-accent'}`} />
    {!horizontal && <span aria-hidden className={`pointer-events-none absolute left-1/2 top-1/2 h-7 w-[3px] -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-surface ${dragging ? 'bg-accent' : 'bg-ink-3 group-hover:bg-accent group-focus:bg-accent'}`} />}
  </div>
}
