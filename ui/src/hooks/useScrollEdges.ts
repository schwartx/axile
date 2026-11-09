import { useLayoutEffect, useState, type RefObject } from 'react'

/** 跟踪滚动容器两端是否还有被裁掉的内容，用于驱动边缘渐隐提示。 */
export function useScrollEdges(ref: RefObject<HTMLElement | null>): {
  up: boolean
  down: boolean
  overflowing: boolean
  measured: boolean
  scrollHeight: number
  clientHeight: number
} {
  const [edges, setEdges] = useState({ up: false, down: false, overflowing: false, measured: false, scrollHeight: 0, clientHeight: 0 })
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const content = el.firstElementChild
    const update = () => {
      const remaining = el.scrollHeight - el.clientHeight - el.scrollTop
      const next = {
        up: el.scrollTop > 1,
        down: remaining > 1,
        overflowing: el.scrollHeight - el.clientHeight > 1,
        measured: true,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      }
      setEdges((prev) => (
        prev.up === next.up
        && prev.down === next.down
        && prev.overflowing === next.overflowing
        && prev.measured === next.measured
        && prev.scrollHeight === next.scrollHeight
        && prev.clientHeight === next.clientHeight
          ? prev
          : next
      ))
    }
    update()
    el.addEventListener('scroll', update, { passive: true })
    const observer = new ResizeObserver(update)
    observer.observe(el)
    if (content) observer.observe(content)
    return () => {
      el.removeEventListener('scroll', update)
      observer.disconnect()
    }
  }, [ref])
  return edges
}
