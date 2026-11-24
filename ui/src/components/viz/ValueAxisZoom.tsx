import { useRef } from 'react'
import { scaleValueRange, type ValueRange } from '@/components/viz/chartAxis'

export function ValueAxisZoom({ label, testId, top, height, width, getRange, onChange }: {
  label: string; testId: string; top: number; height: number; width: number
  getRange: () => ValueRange | null; onChange: (range: ValueRange | null) => void
}) {
  const drag = useRef<{ y: number; range: ValueRange } | null>(null)
  return <div role="group" tabIndex={0} aria-label={label} aria-keyshortcuts="+ - 0" data-testid={testId}
    className="absolute right-0 cursor-ns-resize touch-none focus-visible:outline-accent" style={{ top, height, width }}
    onPointerDown={event => {
      if (!event.isPrimary || event.button !== 0) return
      const range = getRange()
      if (!range) return
      drag.current = { y: event.clientY, range }
      event.currentTarget.setPointerCapture(event.pointerId)
    }}
    onPointerMove={event => {
      if (!drag.current) return
      const { y, range } = drag.current
      onChange(scaleValueRange(range, Math.exp(Math.max(-7, Math.min(7, (event.clientY - y) * 0.01)))))
    }}
    onPointerUp={event => {
      drag.current = null
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    }}
    onPointerCancel={() => { drag.current = null }} onLostPointerCapture={() => { drag.current = null }}
    onDoubleClick={() => onChange(null)}
    onKeyDown={event => {
      if (event.key === '0') { event.preventDefault(); onChange(null); return }
      if (event.key !== '+' && event.key !== '=' && event.key !== '-') return
      event.preventDefault()
      const range = getRange()
      if (range) onChange(scaleValueRange(range, event.key === '-' ? 1.4 : 0.7))
    }} />
}
