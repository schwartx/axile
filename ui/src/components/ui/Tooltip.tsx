import { cloneElement, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import type { ReactElement, ReactNode, Ref } from 'react'
import { createPortal } from 'react-dom'

/** 悬浮卡相对锚点的定位（fixed，随滚动/缩放实时重算）。 */
interface TipPos {
  /** 锚点水平中点的视口坐标（卡片以此为中心，再做视口夹取）。 */
  centerX: number
  /** 锚点上/下边缘视口坐标，供上翻/下落分别定位。 */
  anchorTop: number
  anchorBottom: number
  /** 'up' 卡在锚点上方，'down' 落在下方。 */
  placement: 'up' | 'down'
}

/** 锚点子元素可被注入的事件/引用属性（``cloneElement`` 用，避免额外 DOM 包裹）。 */
interface AnchorProps {
  ref?: Ref<HTMLElement>
  onMouseEnter?: (e: unknown) => void
  onMouseLeave?: (e: unknown) => void
  onFocus?: (e: unknown) => void
  onBlur?: (e: unknown) => void
}

const GAP = 8
const EDGE = 8

/**
 * 通用悬浮卡（hover / focus 触发的富文本浮层）。
 *
 * 定位范式与 :func:`Select` 一致：内容经 ``createPortal`` 挂到 ``document.body`` 并按
 * 锚点矩形 ``fixed`` 定位，故不受祖先 ``overflow`` 裁剪；默认贴在锚点上方，上方空间不足时
 * 下落，水平方向按视口边缘夹取避免溢出。锚点经 ``cloneElement`` 注入引用与事件、**不额外
 * 包裹 DOM**（可直接套在 flex 子项等布局敏感元素上）。样式仅取 ``theme.css`` 语义 token，
 * 明暗主题自动跟随；入场动效复用 ``select-pop-in``，``prefers-reduced-motion`` 由 CSS 兜底。
 * 默认纯展示；interactive 模式允许移入操作，并提供 150ms 离开缓冲。
 *
 * Parameters
 * ----------
 * content : ReactNode
 *     悬浮卡内容，任意富文本节点（本组件不预设结构，故为「通用」）。
 * children : ReactElement
 *     锚点元素，须为单个可接收 ``ref`` 的元素（如 ``span`` / ``div``）。
 * delay : number, default 140
 *     指针进入到显示的延迟（毫秒），抑制掠过时的闪烁；键盘聚焦同样走此延迟。
 * arrow : boolean, default false
 *     是否画一个指向锚点中心的小箭头（卡与「这一段」的连线感）；箭头随水平夹取偏移
 *     实时对准锚点中心，而非卡片中心。
 */
export function Tooltip({
  content,
  children,
  delay = 140,
  arrow = false,
  interactive = false,
}: {
  content: ReactNode
  children: ReactElement
  delay?: number
  arrow?: boolean
  /** 允许进入浮层操作，离开后延迟关闭。 */
  interactive?: boolean
}): ReactElement {
  const id = useId()
  const anchorRef = useRef<HTMLElement | null>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const timer = useRef<number | undefined>(undefined)
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<TipPos | null>(null)
  const [dx, setDx] = useState(0)
  /** 箭头相对卡片左缘的水平位置（px），始终对准锚点中心并留边不脱卡。 */
  const [caretLeft, setCaretLeft] = useState(0)

  /** 计算定位：默认上方，上方空间不足时下落。 */
  const place = useCallback(() => {
    const el = anchorRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const placement: 'up' | 'down' = r.top > 140 ? 'up' : 'down'
    setPos({ centerX: r.left + r.width / 2, anchorTop: r.top, anchorBottom: r.bottom, placement })
  }, [])

  const show = useCallback(() => {
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      place()
      setDx(0)
      setOpen(true)
    }, delay)
  }, [delay, place])

  const hide = useCallback(() => {
    window.clearTimeout(timer.current)
    if (interactive) timer.current = window.setTimeout(() => setOpen(false), 150)
    else setOpen(false)
  }, [interactive])

  useEffect(() => () => window.clearTimeout(timer.current), [])

  useEffect(() => {
    if (!open) return
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        window.clearTimeout(timer.current)
        setOpen(false)
      }
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [open])

  // 打开时：定位随滚动/缩放实时重算（capture 捕获任意祖先滚动）。
  useLayoutEffect(() => {
    if (!open) return
    place()
    const on = () => place()
    window.addEventListener('scroll', on, true)
    window.addEventListener('resize', on)
    return () => {
      window.removeEventListener('scroll', on, true)
      window.removeEventListener('resize', on)
    }
  }, [open, place])

  // 卡片渲染后按实测宽度做水平夹取，越界则整体回推入视口。
  useLayoutEffect(() => {
    if (!open || !pos) return
    const card = cardRef.current
    if (!card) return
    const w = card.offsetWidth
    const half = w / 2
    const leftEdge = pos.centerX - half
    const rightEdge = pos.centerX + half
    let shift = 0
    if (leftEdge < EDGE) shift = EDGE - leftEdge
    else if (rightEdge > window.innerWidth - EDGE) shift = window.innerWidth - EDGE - rightEdge
    if (shift !== dx) setDx(shift)
    // 箭头对准锚点中心（= 卡中心 - 夹取偏移），并留 12px 边避免脱出卡角。
    const caret = Math.max(12, Math.min(w - 12, half - shift))
    if (caret !== caretLeft) setCaretLeft(caret)
  }, [open, pos, dx, caretLeft])

  const props = children.props as AnchorProps
  const anchor = cloneElement(children, {
    ref: (node: HTMLElement | null) => {
      anchorRef.current = node
      const r = props.ref
      if (typeof r === 'function') r(node)
      else if (r) (r as { current: HTMLElement | null }).current = node
    },
    onMouseEnter: (e: unknown) => {
      props.onMouseEnter?.(e)
      show()
    },
    onMouseLeave: (e: unknown) => {
      props.onMouseLeave?.(e)
      hide()
    },
    onFocus: (e: unknown) => {
      props.onFocus?.(e)
      show()
    },
    onBlur: (e: unknown) => {
      props.onBlur?.(e)
      hide()
    },
    'aria-describedby': open ? id : undefined,
  } as Partial<AnchorProps>)

  return (
    <>
      {anchor}
      {open &&
        pos &&
        createPortal(
          <div
            className={`${interactive ? 'pointer-events-auto' : 'pointer-events-none'} fixed z-[60] w-max max-w-[min(320px,90vw)]`}
            onMouseEnter={() => window.clearTimeout(timer.current)}
            onMouseLeave={hide}
            onFocus={() => window.clearTimeout(timer.current)}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) hide()
            }}
            onClick={interactive ? (event) => event.stopPropagation() : undefined}
            style={{
              left: pos.centerX,
              transform: `translateX(calc(-50% + ${dx}px))`,
              ...(pos.placement === 'up'
                ? { bottom: window.innerHeight - pos.anchorTop + GAP }
                : { top: pos.anchorBottom + GAP }),
            }}
          >
            <div
              ref={cardRef}
              id={id}
              role={interactive ? "group" : "tooltip"}
              className="select-pop-in relative max-w-[min(320px,90vw)] rounded-[11px] border border-line bg-surface px-3 py-2.5 text-[13.5px] leading-relaxed shadow-[0_12px_32px_rgba(0,0,0,0.16)]"
            >
              {content}
              {arrow && (
                <span
                  aria-hidden
                  className={`absolute h-2.5 w-2.5 rotate-45 bg-surface ${
                    pos.placement === 'up'
                      ? '-bottom-[5px] border-r border-b border-line'
                      : '-top-[5px] border-l border-t border-line'
                  }`}
                  style={{ left: caretLeft, marginLeft: -5 }}
                />
              )}
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}
