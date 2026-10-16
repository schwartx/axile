import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'

/** 同一内容槽的交接：控件留在槽外，退场内容不可交互，刷新同一视图不重播。 */
export function ContentSwap({ identity, children }: { identity: string; children: ReactNode }) {
  const slotRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const committed = useRef({ identity, children, height: 0 })
  const animations = useRef<Animation[]>([])
  const [outgoing, setOutgoing] = useState<{ children: ReactNode; height: number } | null>(null)

  useLayoutEffect(() => {
    const previous = committed.current
    const content = contentRef.current!
    committed.current = { identity, children, height: content.offsetHeight }
    if (previous.identity === identity) return
    animations.current.forEach(animation => animation.cancel())
    animations.current = []
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setOutgoing(null)
      return
    }
    setOutgoing({ children: previous.children, height: previous.height })
  }, [identity, children])

  useLayoutEffect(() => {
    const content = contentRef.current!
    // 异步数据和窗口尺寸变化只更新自然高度，不触发交接。
    const observer = new ResizeObserver(() => { committed.current.height = content.offsetHeight })
    observer.observe(content)
    return () => observer.disconnect()
  }, [])

  useLayoutEffect(() => {
    if (!outgoing) return
    const slot = slotRef.current!
    const content = contentRef.current!
    const style = getComputedStyle(slot)
    const duration = Number.parseFloat(style.getPropertyValue('--motion-content-swap-ms'))
    const timing = { duration, easing: style.getPropertyValue('--motion-content-swap-ease').trim() }
    // 旧层保持不透明，新层带背景覆盖；两层同时淡化会让交接中段发暗。
    const runs = [
      content.animate([{ opacity: 0 }, { opacity: 1 }], timing),
      slot.animate([{ height: `${outgoing.height}px` }, { height: `${content.offsetHeight}px` }], timing),
    ]
    animations.current = runs
    void Promise.all(runs.map(animation => animation.finished)).then(() => {
      setOutgoing(current => current === outgoing ? null : current)
    }).catch(() => { /* 连续切换或卸载会取消上一段交接。 */ })
    return () => runs.forEach(animation => animation.cancel())
  }, [outgoing])

  return <div ref={slotRef} className="content-swap relative min-w-0 overflow-hidden" data-content-swap>
    {outgoing && <div aria-hidden inert className="pointer-events-none absolute inset-x-0 top-0" data-swap-outgoing>{outgoing.children}</div>}
    <div ref={contentRef} className="relative min-w-0 bg-surface" data-swap-current>{children}</div>
  </div>
}
