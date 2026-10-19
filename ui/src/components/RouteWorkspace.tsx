import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { useLocation, useNavigationType } from 'react-router'

interface Position { top: number; left: number }
const positions = new Map<string, Position>()

/** 每个页面拥有独立滚动容器；新页面自然从顶部开始，历史返回恢复原位置。 */
export function RouteWorkspace({ fullBleed, children }: { fullBleed: boolean; children: ReactNode }) {
  const ref = useRef<HTMLElement>(null)
  const location = useLocation()
  const navigation = useNavigationType()
  useLayoutEffect(() => {
    const main = ref.current
    if (!main) return
    // PUSH 的新页面由新 DOM 保证 scrollTop=0；只有 POP 才应用历史位置。
    const previous = navigation === 'POP' ? positions.get(location.key) : undefined
    if (previous) main.scrollTo({ ...previous, behavior: 'instant' })
    const save = () => {
      positions.delete(location.key)
      positions.set(location.key, { top: main.scrollTop, left: main.scrollLeft })
      while (positions.size > 100) positions.delete(positions.keys().next().value!)
    }
    main.addEventListener('scroll', save, { passive: true })
    return () => {
      save()
      main.removeEventListener('scroll', save)
    }
  }, [location.key, navigation])

  return <main ref={ref}
    className={`app-workspace h-full min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable] ${fullBleed ? '' : 'px-4 md:px-5'}`}>
    <div className={`mx-auto h-full w-full max-w-[2176px] ${fullBleed ? '' : 'pb-16 pt-5'}`}>
      {children}
    </div>
  </main>
}
