import type { RefObject } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'

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
