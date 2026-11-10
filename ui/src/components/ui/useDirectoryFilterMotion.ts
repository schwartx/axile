import { useCallback, useEffect, useLayoutEffect, useRef, type RefObject } from 'react'

/** 仅在输入筛选时交接可见行的位置；浏览器播放动画，连续输入直接接续。 */
export function useDirectoryFilterMotion(container: RefObject<HTMLDivElement | null>, path: string | null | undefined, value: string, open: boolean) {
  const snapshot = useRef<{ path: typeof path; rows: Map<string, { top: number; opacity: number }> } | null>(null)
  const animations = useRef<Animation[]>([])
  const stop = useCallback(() => {
    animations.current.forEach(animation => animation.cancel())
    animations.current = []
  }, [])
  const capture = useCallback(() => {
    const element = container.current
    if (!element) return []
    const rows = Array.from(element.querySelectorAll<HTMLButtonElement>('[data-directory-path]'))
    if (!rows.length) return []
    // h-9 + mt-0.5；只读取视口附近的几行，避免逐个测量整个目录。
    const pitch = rows[0].offsetHeight + 2
    const start = Math.max(0, Math.floor(element.scrollTop / pitch) - 1)
    const bounds = element.getBoundingClientRect()
    return rows.slice(start, start + Math.ceil(element.clientHeight / pitch) + 3).flatMap(row => {
      const rect = row.getBoundingClientRect()
      return rect.bottom > bounds.top && rect.top < bounds.bottom ? [{ row, top: rect.top }] : []
    })
  }, [container])
  const prepare = useCallback(() => {
    snapshot.current = null
    if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      snapshot.current = { path, rows: new Map(capture().map(({ row, top }) => [row.dataset.directoryPath!, { top, opacity: Number(getComputedStyle(row).opacity) }])) }
    }
    stop()
  }, [path, capture, stop])

  useLayoutEffect(() => {
    const previous = snapshot.current
    snapshot.current = null
    if (!open || previous?.path !== path || !previous || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      stop()
      return
    }
    const rows = capture()
    // 先批量读取，再播放，避免动画写入与布局读取交错。
    for (const { row, top } of rows) {
      const before = previous.rows.get(row.dataset.directoryPath!)
      if (row.hasAttribute('data-current-directory')) continue
      const frames = before
        ? [{ transform: `translateY(${before.top - top}px)`, opacity: before.opacity }, { transform: 'translateY(0)', opacity: 1 }]
        : [{ opacity: 0 }, { opacity: 1 }]
      if (before && Math.abs(before.top - top) < 0.5 && before.opacity === 1) continue
      animations.current.push(row.animate(frames, { duration: 140, easing: 'cubic-bezier(.32,.72,0,1)' }))
    }
  }, [value, path, open, capture, stop])
  useEffect(() => {
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)')
    const changed = () => { snapshot.current = null; stop() }
    preference.addEventListener('change', changed)
    return () => preference.removeEventListener('change', changed)
  }, [stop])
  useLayoutEffect(() => stop, [stop])
  return prepare
}
