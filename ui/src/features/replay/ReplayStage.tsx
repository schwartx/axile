import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ResizeHandle } from '@/components/ui/ResizeHandle'

const HEIGHT_KEY = 'axon.replay.dockHeight'
const DEFAULT_HEIGHT = 128

export function ReplayStage({ children }: { children: ReactNode }) {
  const stage = useRef<HTMLDivElement>(null)
  const [height, setHeight] = useState(0)
  const [dockHeight, setDockHeight] = useState(() => {
    const stored = typeof window === 'undefined' ? null : window.localStorage.getItem(HEIGHT_KEY)
    const value = Number(stored)
    return stored != null && Number.isFinite(value) && value >= 96 ? value : DEFAULT_HEIGHT
  })
  const [resizing, setResizing] = useState(false)
  useEffect(() => {
    const element = stage.current
    if (!element) return
    const measure = () => setHeight(element.getBoundingClientRect().height)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const min = height > 0 ? Math.min(96, height / 3) : 96
  const max = height > 0 ? Math.max(min, height - 245) : DEFAULT_HEIGHT
  const clamp = (value: number) => Math.min(max, Math.max(min, value))
  const persist = (value: number) => {
    const next = clamp(value)
    setDockHeight(next)
    window.localStorage.setItem(HEIGHT_KEY, String(next))
  }
  return <div ref={stage} className={`replay-stage ${resizing ? 'select-none' : ''}`}
    style={{ gridTemplateRows: `minmax(0, 1fr) 5px ${clamp(dockHeight)}px` }}>
    {children}
    <ResizeHandle aria-label="调整图表与回放明细的高度" orientation="horizontal"
      value={clamp(dockHeight)} min={min} max={max} step={-24}
      title="上下拖动调整明细高度 · 双击恢复默认"
      className="replay-dock-resize" valueAt={y => {
        const bounds = stage.current?.getBoundingClientRect()
        return bounds ? clamp(bounds.bottom - y - 2.5) : clamp(dockHeight)
      }} onChange={setDockHeight} onCommit={persist} onReset={() => persist(DEFAULT_HEIGHT)} onResizeChange={setResizing}
    />
  </div>
}
