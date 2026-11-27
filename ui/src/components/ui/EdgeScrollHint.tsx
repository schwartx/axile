import { useLayoutEffect, useState, type RefObject } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { useScrollEdges } from '@/hooks/useScrollEdges'

/** 溢出端的方向提示：渐隐带 + 可点击箭头，点击按接近一屏的步长朝该端滚动。 */
export function EdgeScrollHint({ edge, visible, target, label = '导航', className = 'from-surface via-surface/80' }: {
  edge: 'up' | 'down'
  visible: boolean
  target: RefObject<HTMLElement | null>
  label?: string
  className?: string
}) {
  const Icon = edge === 'up' ? ChevronUp : ChevronDown
  const nudge = () => {
    const el = target.current
    if (!el) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    el.scrollBy({
      top: (edge === 'up' ? -1 : 1) * el.clientHeight * 0.8,
      behavior: reduced ? 'auto' : 'smooth',
    })
  }
  return (
    <div
      aria-hidden={!visible}
      className={`pointer-events-none absolute inset-x-0 z-[2] flex h-5 to-transparent transition-opacity duration-200 motion-reduce:transition-none ${className} ${
        edge === 'up' ? 'top-0 items-start bg-gradient-to-b' : 'bottom-0 items-end bg-gradient-to-t'
      } ${visible ? 'opacity-100' : 'opacity-0'}`}
    >
      <button
        type="button"
        aria-label={`向${edge === 'up' ? '上' : '下'}滚动${label}`}
        tabIndex={visible ? 0 : -1}
        onClick={nudge}
        className={`mx-auto px-3 text-ink-3 transition-colors duration-150 hover:text-ink-1 ${
          visible ? 'pointer-events-auto' : 'pointer-events-none'
        }`}
      >
        <Icon size={14} aria-hidden />
      </button>
    </div>
  )
}

/** 与滚动区同处一个定位容器，提示覆盖正文边缘并为固定表头留位。 */
export function EdgeScrollHints({ target, label, stickyHeader = false, className }: {
  target: RefObject<HTMLDivElement | null>
  label: string
  stickyHeader?: boolean
  className?: string
}) {
  const edges = useScrollEdges(target)
  const [bounds, setBounds] = useState({ top: 0, left: 0, width: 0, height: 0 })

  useLayoutEffect(() => {
    const viewport = target.current
    if (!viewport) return
    const header = stickyHeader ? viewport.querySelector('thead') : null
    const measure = () => {
      const headerHeight = header?.offsetHeight ?? 0
      setBounds({
        top: viewport.clientTop + headerHeight,
        left: viewport.clientLeft,
        width: viewport.clientWidth,
        height: Math.max(0, viewport.clientHeight - headerHeight),
      })
    }
    measure()
    // 观察内容宽度，滚动条出现或消失时仍能对齐滚动区。
    const observer = new ResizeObserver(measure)
    observer.observe(viewport)
    if (header) observer.observe(header)
    return () => observer.disconnect()
  }, [target, stickyHeader])

  return <div className="pointer-events-none absolute" style={bounds}>
    <EdgeScrollHint edge="up" visible={edges.up && bounds.height > 0} target={target} label={label} className={className} />
    <EdgeScrollHint edge="down" visible={edges.down && bounds.height > 0} target={target} label={label} className={className} />
  </div>
}
