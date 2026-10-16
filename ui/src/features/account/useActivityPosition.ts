import { useCallback, useLayoutEffect, useRef } from 'react'
import type { ActivityPosition } from '@/features/account/activityViewMemory'

/** 内容就绪的同一帧恢复滚动；骨架及旧页签的滚动事件不覆盖记忆。 */
export function useActivityPosition(key: string, positions: Map<string, ActivityPosition>, ready: boolean, open: boolean) {
  const ref = useRef<HTMLDivElement>(null)
  const active = useRef<string | null>(null)
  const capture = useCallback(() => {
    const node = ref.current
    if (!node || active.current === null) return
    const horizontal = node.querySelector<HTMLElement>('[data-swap-current] [data-record-scroll]')
    positions.set(active.current, { top: node.scrollTop, left: horizontal?.scrollLeft ?? 0 })
  }, [positions])
  useLayoutEffect(() => {
    const node = ref.current
    if (!node) return
    if (!open) { capture(); return }
    if (!ready) { active.current = null; return }
    const position = positions.get(key)
    node.scrollTop = position?.top ?? 0
    const horizontal = node.querySelector<HTMLElement>('[data-swap-current] [data-record-scroll]')
    if (horizontal) horizontal.scrollLeft = position?.left ?? 0
    active.current = key
  }, [key, ready, open, positions, capture])
  return { ref, capture, onScrollCapture: () => { if (open && ready && active.current === key) capture() } }
}
