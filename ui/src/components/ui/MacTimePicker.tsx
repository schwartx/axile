/**
 * Mac 闹钟风格的时刻选择：双段大数字胶囊（时 | 分）。
 *
 * 点按打开双列滚轮；菜单经 portal 挂到 body，避免被祖先 overflow 裁切
 * （与 Select 同款策略）。着色仅用 theme token。
 */

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactElement,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { NumberTicker } from '@/components/ui/NumberTicker'

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function parseHm(value: string): { h: number; m: number } | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(value.trim())
  if (!m) return null
  const h = Number(m[1])
  const min = Number(m[2])
  if (h < 0 || h > 23 || min < 0 || min > 59) return null
  return { h, m: min }
}

type Part = 'h' | 'm'

const ITEM_H = 36
const VISIBLE = 5
const GAP = 8
const PART_FORMAT = { minimumIntegerDigits: 2, maximumFractionDigits: 0, useGrouping: false } as const

/** 大幅滚动只走一格，触控板细小位移累计后步进；不排队，避免停手后继续跳。 */
function useWheelStep(ref: RefObject<HTMLDivElement | null>, onStep: (direction: number) => void, enabled = true) {
  const latest = useRef(onStep)
  useLayoutEffect(() => { latest.current = onStep }, [onStep])
  useEffect(() => {
    const element = ref.current
    if (!element || !enabled) return
    let accumulated = 0
    let direction = 0
    let lastEvent = -Infinity
    let lastStep = -Infinity
    const wheel = (event: WheelEvent) => {
      if (event.ctrlKey || event.metaKey || !event.deltaY || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return
      event.preventDefault()
      const now = performance.now()
      const nextDirection = Math.sign(event.deltaY)
      if (now - lastEvent > 200 || nextDirection !== direction) {
        accumulated = 0
        lastStep = -Infinity
      }
      direction = nextDirection
      lastEvent = now
      accumulated += event.deltaY * (event.deltaMode === 0 ? 1 : 32)
      if (Math.abs(accumulated) < 32 || now - lastStep < 80) return
      accumulated = 0
      lastStep = now
      latest.current(direction)
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [ref, enabled])
}

interface MenuPos {
  top: number
  left: number
  placement: 'down' | 'up'
}

/**
 * 单列数字滚轮（中间槽高亮）。
 */
function WheelColumn({
  compact = false,
  min = 0,
  max,
  value,
  onChange,
  labelledBy,
}: {
  compact?: boolean
  min?: number
  max: number
  value: number
  onChange: (n: number) => void
  labelledBy: string
}) {
  const itemHeight = compact ? 32 : ITEM_H
  const scrollerRef = useRef<HTMLDivElement>(null)
  const scrollTarget = useRef<number | null>(null)
  const scrolledValue = useRef<number | null>(null)
  const initialized = useRef(false)
  const drag = useRef<{ y: number; top: number } | null>(null)
  const dragged = useRef(false)

  useWheelStep(scrollerRef, direction => {
    onChange(Math.max(min, Math.min(max, value + direction)))
  })

  useEffect(() => {
    const el = scrollerRef.current
    const fromScroll = scrolledValue.current === value
    scrolledValue.current = null
    if (!el || drag.current || fromScroll) return
    const top = (value - min) * itemHeight
    const smooth = initialized.current && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
    initialized.current = true
    if (Math.abs(el.scrollTop - top) > 1) {
      scrollTarget.current = top
      el.scrollTo({ top, behavior: smooth ? 'smooth' : 'instant' })
    }
  }, [value, min, itemHeight])

  const settle = () => {
    const el = scrollerRef.current
    if (!el) return
    const idx = Math.max(min, Math.min(max, min + Math.round(el.scrollTop / itemHeight)))
    scrollTarget.current = (idx - min) * itemHeight
    el.scrollTo({ top: scrollTarget.current, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })
    if (idx !== value) onChange(idx)
  }

  const visible = compact ? 3 : VISIBLE
  const pad = Math.floor(visible / 2)
  const finishDrag = () => {
    if (!drag.current) return
    drag.current = null
    settle()
  }

  return (
    <div className={`relative ${compact ? 'w-12' : 'w-[64px]'}`}>
      <div
        aria-hidden
        style={{ height: itemHeight }}
        className="pointer-events-none absolute inset-x-0.5 top-1/2 -translate-y-1/2 rounded-[9px] bg-ink-1"
      />
      <div
        ref={scrollerRef}
        role="listbox"
        aria-labelledby={labelledBy}
        aria-activedescendant={`${labelledBy}-${value}`}
        tabIndex={0}
        style={{ height: itemHeight * visible }}
        className="relative cursor-grab select-none overflow-y-auto overscroll-contain active:cursor-grabbing [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        onPointerDown={(event) => {
          dragged.current = false
          scrollTarget.current = null
          event.currentTarget.scrollTo({ top: event.currentTarget.scrollTop, behavior: 'instant' })
          if (event.pointerType !== 'mouse' || event.button !== 0) return
          const target = event.target as HTMLElement
          event.preventDefault()
          event.currentTarget.focus({ preventScroll: true })
          drag.current = { y: event.clientY, top: event.currentTarget.scrollTop }
          target.setPointerCapture(event.pointerId)
        }}
        onPointerMove={(event) => {
          if (!drag.current) return
          const distance = drag.current.y - event.clientY
          if (!dragged.current && Math.abs(distance) < 4) return
          dragged.current = true
          event.currentTarget.scrollTop = drag.current.top + distance
        }}
        onClickCapture={(event) => {
          if (!dragged.current || event.detail === 0) return
          event.preventDefault()
          event.stopPropagation()
        }}
        onScroll={() => {
          const el = scrollerRef.current
          if (!el) return
          if (scrollTarget.current !== null) {
            if (Math.abs(el.scrollTop - scrollTarget.current) < 1) scrollTarget.current = null
            return
          }
          const idx = Math.max(min, Math.min(max, min + Math.round(el.scrollTop / itemHeight)))
          if (idx !== value) {
            scrolledValue.current = idx
            onChange(idx)
          }
        }}
        onKeyDown={(e) => {
          if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault()
            onChange(e.key === 'ArrowUp' ? Math.max(min, value - 1) : Math.min(max, value + 1))
          }
        }}
        onPointerUp={() => { drag.current = null; settle() }}
        onPointerCancel={finishDrag}
        onLostPointerCapture={finishDrag}
      >
        {Array.from({ length: pad }, (_, i) => (
          <div key={`t-${i}`} style={{ height: itemHeight }} className="shrink-0" aria-hidden />
        ))}
        {Array.from({ length: max - min + 1 }, (_, i) => i + min).map(n => (
          <button
            key={n}
            type="button"
            id={`${labelledBy}-${n}`}
            role="option"
            aria-selected={n === value}
            style={{ height: itemHeight }}
            className={`flex w-full shrink-0 cursor-grab items-center justify-center font-mono ${compact ? 'text-[16px]' : 'text-[21px]'} font-[640] tabular-nums active:cursor-grabbing ${
              n === value ? 'text-surface' : 'text-ink-3'
            }`}
            onClick={() => onChange(n)}
          >
            {pad2(n)}
          </button>
        ))}
        {Array.from({ length: pad }, (_, i) => (
          <div key={`b-${i}`} style={{ height: itemHeight }} className="shrink-0" aria-hidden />
        ))}
      </div>
      <div aria-hidden className={`pointer-events-none absolute inset-x-0 top-0 ${compact ? 'h-6' : 'h-12'} bg-gradient-to-b from-surface to-transparent`} />
      <div aria-hidden className={`pointer-events-none absolute inset-x-0 bottom-0 ${compact ? 'h-6' : 'h-12'} bg-gradient-to-t from-surface to-transparent`} />
    </div>
  )
}

/**
 * Mac 风格时刻选择器。
 */
export function MacTimePicker({
  value,
  onChange,
  className = '',
  min = '00:00',
  max = '23:59',
  ariaLabel = '时刻',
  compact = false,
}: {
  value: string
  onChange: (value: string) => void
  className?: string
  min?: string
  max?: string
  ariaLabel?: string
  compact?: boolean
}): ReactElement {
  const parsed = parseHm(value)
  const lower = parseHm(min) ?? { h: 0, m: 0 }
  const upper = parseHm(max) ?? { h: 23, m: 59 }
  const minTime = lower.h * 60 + lower.m
  const maxTime = Math.max(minTime, upper.h * 60 + upper.m)
  const current = Math.max(minTime, Math.min(maxTime, (parsed?.h ?? 8) * 60 + (parsed?.m ?? 0)))
  const h = Math.floor(current / 60)
  const m = current % 60
  const minHour = lower.h
  const maxHour = Math.floor(maxTime / 60)
  const minMinute = h === minHour ? lower.m : 0
  const maxMinute = h === maxHour ? maxTime % 60 : 59
  const empty = !parsed

  const [part, setPart] = useState<Part>(minHour === maxHour ? 'm' : 'h')
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<MenuPos | null>(null)
  const triggerRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const baseId = useId()

  const commit = useCallback(
    (nh: number, nm: number) => {
      const total = Math.max(minTime, Math.min(maxTime, nh * 60 + nm))
      onChange(`${pad2(Math.floor(total / 60))}:${pad2(total % 60)}`)
    },
    [onChange, minTime, maxTime],
  )

  const ensureValue = () => {
    if (empty) commit(h, m)
  }

  const bump = (delta: number) => {
    ensureValue()
    if (part === 'h') {
      const count = maxHour - minHour + 1
      commit(minHour + (h - minHour + delta + count) % count, m)
    } else {
      const count = maxMinute - minMinute + 1
      commit(h, minMinute + (m - minMinute + delta + count) % count)
    }
  }

  useWheelStep(triggerRef, direction => {
    commit(
      part === 'h' ? Math.max(minHour, Math.min(maxHour, h + direction)) : h,
      part === 'm' ? Math.max(minMinute, Math.min(maxMinute, m + direction)) : m,
    )
  }, !open)

  const place = useCallback(() => {
    const el = triggerRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const menuH = compact ? 136 : 220
    const menuW = compact ? 128 : 160
    const spaceBelow = window.innerHeight - r.bottom
    const placement: 'down' | 'up' = spaceBelow < menuH + GAP && r.top > spaceBelow ? 'up' : 'down'
    let left = r.right - menuW
    left = Math.max(8, Math.min(left, window.innerWidth - menuW - 8))
    const top = placement === 'down' ? r.bottom + GAP : r.top - GAP - menuH
    setPos({ top, left, placement })
  }, [compact])

  const openMenu = (which: Part) => {
    setPart(which)
    ensureValue()
    place()
    setOpen(true)
  }

  useLayoutEffect(() => {
    if (!open) return
    place()
    const onScroll = () => place()
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onScroll)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onScroll)
    }
  }, [open, place])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (triggerRef.current?.contains(t) || menuRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  /** 一体胶囊：当前段反色突出，空值两段弱化。 */
  const segClass = (which: Part) => {
    if (empty && !open) return 'bg-transparent text-ink-3'
    if (part === which) return 'bg-ink-1 text-surface'
    return 'bg-transparent text-ink-1'
  }

  const menu =
    open &&
    pos &&
    createPortal(
      <div
        ref={menuRef}
        role="dialog"
        aria-label="选择时刻"
        onKeyDown={(event) => {
          if (event.key !== 'Escape') return
          event.preventDefault()
          event.stopPropagation()
          setOpen(false)
          triggerRef.current?.focus()
        }}
        className={`select-pop-in fixed z-50 flex ${compact ? 'gap-1.5 p-2' : 'gap-2 p-2.5'} rounded-[14px] border border-line bg-surface shadow-[0_12px_32px_rgba(0,0,0,0.18)]`}
        style={{ top: pos.top, left: pos.left }}
      >
        <div className="flex flex-col items-center">
          <span id={`${baseId}-h`} className="mb-0.5 text-[12px] font-semibold tracking-wide text-ink-3">
            时
          </span>
          <WheelColumn
            compact={compact}
            min={minHour}
            max={maxHour}
            value={h}
            labelledBy={`${baseId}-h`}
            onChange={(nh) => {
              setPart('h')
              commit(nh, m)
            }}
          />
        </div>
        <div className="w-px self-stretch bg-line" aria-hidden />
        <div className="flex flex-col items-center">
          <span id={`${baseId}-m`} className="mb-0.5 text-[12px] font-semibold tracking-wide text-ink-3">
            分
          </span>
          <WheelColumn
            compact={compact}
            min={minMinute}
            max={maxMinute}
            value={m}
            labelledBy={`${baseId}-m`}
            onChange={(nm) => {
              setPart('m')
              commit(h, nm)
            }}
          />
        </div>
      </div>,
      triggerRef.current?.closest(':popover-open, dialog[open]') ?? document.body,
    )

  return (
    <div className={`inline-flex ${className}`}>
      <div
        ref={triggerRef}
        role="group"
        aria-label={ariaLabel}
        aria-expanded={open}
        tabIndex={0}
        className={`inline-flex overflow-hidden ${compact ? 'rounded-[9px]' : 'rounded-[12px]'} bg-fill outline-none focus-visible:shadow-[0_0_0_2px_var(--color-accent)]`}
        onKeyDown={(e) => {
          if (e.key === 'ArrowUp') {
            e.preventDefault()
            bump(1)
          } else if (e.key === 'ArrowDown') {
            e.preventDefault()
            bump(-1)
          } else if (e.key === 'ArrowLeft') {
            e.preventDefault()
            setPart(minHour === maxHour ? 'm' : 'h')
          } else if (e.key === 'ArrowRight') {
            e.preventDefault()
            setPart('m')
          } else if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            if (open) setOpen(false)
            else openMenu(part)
          } else if (e.key === 'Escape' && open) {
            e.preventDefault()
            e.stopPropagation()
            setOpen(false)
          }
        }}
      >
        <button
          type="button"
          aria-label="时"
          disabled={minHour === maxHour}
          aria-pressed={part === 'h'}
          onClick={() => openMenu('h')}
          className={`${compact ? 'min-h-8 min-w-9 px-2.5 py-1.5 text-[14px]' : 'min-w-[3.4rem] px-3.5 py-2.5 text-[29px]'} font-mono font-[640] leading-none tabular-nums tracking-tight transition-colors ${segClass('h')}`}
        >
          {empty && !open ? '––' : <NumberTicker className="block" value={h} format={PART_FORMAT} />}
        </button>
        <button
          type="button"
          aria-label="分"
          aria-pressed={part === 'm'}
          onClick={() => openMenu('m')}
          className={`${compact ? 'min-h-8 min-w-9 px-2.5 py-1.5 text-[14px]' : 'min-w-[3.4rem] px-3.5 py-2.5 text-[29px]'} font-mono font-[640] leading-none tabular-nums tracking-tight transition-colors ${segClass('m')}`}
        >
          {empty && !open ? '––' : <NumberTicker className="block" value={m} format={PART_FORMAT} />}
        </button>
      </div>
      {menu}
    </div>
  )
}
