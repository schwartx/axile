import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactElement } from 'react'
import { createPortal } from 'react-dom'

import { OverflowText } from '@/components/ui/OverflowText'

/** 下拉的单个选项：description 为标题下说明，hint 为右侧弱化短信息。 */
export interface SelectOption<T> {
  value: T
  label: string
  description?: string
  hint?: string
}

/** 菜单相对触发器的定位结果（fixed 定位，随滚动/缩放实时重算）。 */
interface MenuPos {
  left: number
  minWidth: number
  maxHeight: number
  /** 'down' 锚在触发器下方，'up' 向上翻。 */
  placement: 'down' | 'up'
  /** 触发器上/下边缘的视口坐标，供 up/down 分别定位。 */
  anchorTop: number
  anchorBottom: number
}

const GAP = 6

/** 触发器不变样式（边框/底色/焦点/圆角）；布局（内距/字号/宽度）由 layout 决定。 */
const TRIGGER_BASE =
  'inline-flex items-center gap-2 rounded-[9px] border border-ink-3/30 contrast:border-border-strong bg-surface ' +
  'text-ink-1 cursor-pointer outline-none transition-colors focus:border-accent ' +
  'disabled:cursor-not-allowed disabled:opacity-45'

/** 未传 className 时的紧凑内联布局（用于句子内嵌的下拉）。 */
const TRIGGER_LAYOUT_DEFAULT = 'px-2.5 py-1.5 text-[14px]'

/**
 * 受控单选下拉框（原生 `<select>` 的替代）。
 *
 * 触发器就地渲染，菜单经 `createPortal` 挂到 `document.body` 并按触发器矩形
 * 定位，故不受祖先 `overflow` 裁剪；空间不足时向上翻。仅用 `theme.css` 的语义
 * token 着色，明暗主题自动跟随。支持键盘（↑/↓/Home/End/Enter/Esc）与可选搜索。
 *
 * 值以 `===` 比较，故 `T` 可为 `number | null` 等，用 `null` 承载「空/不绑定」哨兵。
 * `className` 若传入，将**整体替换**触发器的布局类（内距/字号/宽度/对齐），
 * 例如整行场景传 `"w-full justify-between px-3 py-2 text-[15px]"`；不传则用紧凑内联布局。
 *
 * @typeParam T - 选项值类型。
 */
export function Select<T>({
  value,
  options,
  onChange,
  searchable = false,
  placeholder = '请选择',
  className,
  ariaLabel,
  disabled = false,
  selectedValue,
}: {
  value: T
  options: SelectOption<T>[]
  onChange: (value: T) => void
  searchable?: boolean
  placeholder?: string
  className?: string
  ariaLabel?: string
  disabled?: boolean
  /** 需要把选中值的局部文字单独作为共享元素时，由调用方提供触发器内容。 */
  selectedValue?: ReactNode
}): ReactElement {
  const listId = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const scrollIntentRef = useRef<ScrollLogicalPosition | null>(null)

  // open=意图（决定 pop-in / pop-out），mounted=是否在 DOM。
  // 收起先播退场再卸载，故两态分离，与 AccountActions ⋯ 菜单同范式。
  const [open, setOpen] = useState(false)
  const [mounted, setMounted] = useState(false)
  const [pos, setPos] = useState<MenuPos | null>(null)
  const [query, setQuery] = useState('')
  const [highlight, setHighlight] = useState(0)

  const filtered =
    searchable && query
      ? options.filter((o) => {
          const needle = query.toLowerCase()
          return [o.label, o.description, o.hint].some((text) => text?.toLowerCase().includes(needle))
        })
      : options

  const selected = options.find((o) => o.value === value)

  /** 计算菜单定位：默认向下，下方空间不足且上方更宽敞时向上翻。 */
  const place = useCallback(() => {
    const el = triggerRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const spaceBelow = window.innerHeight - r.bottom
    const spaceAbove = r.top
    const placement: 'down' | 'up' = spaceBelow < 220 && spaceAbove > spaceBelow ? 'up' : 'down'
    const avail = (placement === 'up' ? spaceAbove : spaceBelow) - GAP - 8
    setPos({
      left: r.left,
      minWidth: r.width,
      maxHeight: Math.max(140, Math.min(320, avail)),
      placement,
      anchorTop: r.top,
      anchorBottom: r.bottom,
    })
  }, [])

  const close = useCallback((refocus: boolean) => {
    setOpen(false)
    setQuery('')
    if (refocus) triggerRef.current?.focus()
    // reduce-motion 下 select-pop-out 为 none，onAnimationEnd 不触发，即时卸载兜底。
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) setMounted(false)
  }, [])

  const openMenu = useCallback(() => {
    if (disabled) return
    place()
    scrollIntentRef.current = 'center'
    setHighlight(Math.max(0, options.findIndex((o) => o.value === value)))
    setMounted(true)
    setOpen(true)
  }, [disabled, place, options, value])

  // 打开时：定位随滚动/缩放实时重算（capture 捕获任意祖先滚动）。
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

  // 搜索态：打开即聚焦输入框；仅用户修改查询时重置高亮，保留打开时的已选项。
  useEffect(() => {
    if (open && searchable) inputRef.current?.focus()
  }, [open, searchable])

  // 外部点击关闭（触发器与菜单内部不算外部）。
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (triggerRef.current?.contains(t) || menuRef.current?.contains(t)) return
      close(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open, close])

  // 打开时居中，键盘和搜索仅确保可见；鼠标悬停不驱动滚动。
  useLayoutEffect(() => {
    const block = scrollIntentRef.current
    if (!open || !block) return
    scrollIntentRef.current = null
    document.getElementById(`${listId}-opt-${highlight}`)?.scrollIntoView({ block, behavior: 'instant' })
  }, [open, highlight, query, listId])

  const choose = (opt: SelectOption<T>) => {
    onChange(opt.value)
    close(true)
  }

  /** 打开态的键盘导航；触发器（非搜索）与搜索输入框共用。 */
  const onNavKey = (e: ReactKeyboardEvent) => {
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
      scrollIntentRef.current = 'nearest'
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight((h) => Math.min(filtered.length - 1, h + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight((h) => Math.max(0, h - 1))
    } else if (e.key === 'Home') {
      e.preventDefault()
      setHighlight(0)
    } else if (e.key === 'End') {
      e.preventDefault()
      setHighlight(filtered.length - 1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const opt = filtered[highlight]
      if (opt) choose(opt)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      close(true)
    } else if (e.key === 'Tab') {
      close(false)
    }
  }

  /** 触发器键盘：关闭态回车/空格/↑↓ 打开；打开且非搜索时走导航。 */
  const onTriggerKey = (e: ReactKeyboardEvent) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) {
        e.preventDefault()
        openMenu()
      }
      return
    }
    if (!searchable) onNavKey(e)
  }

  const activeDesc = open ? `${listId}-opt-${highlight}` : undefined

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        aria-activedescendant={!searchable ? activeDesc : undefined}
        className={`${TRIGGER_BASE} ${className ?? TRIGGER_LAYOUT_DEFAULT}`}
        onClick={() => (open ? close(false) : openMenu())}
        onKeyDown={onTriggerKey}
      >
        <span className={`min-w-0 text-left ${selected ? '' : 'text-ink-3'}`}>
          {selectedValue ?? <OverflowText text={selected ? selected.label : placeholder} />}
          {selected?.description && (
            <OverflowText text={selected.description} className="mt-0.5 text-[13px] font-normal text-ink-3" />
          )}
        </span>
        <svg
          viewBox="0 0 12 12"
          aria-hidden
          className={`ml-auto h-3 w-3 flex-none text-ink-3 transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
        >
          <path
            d="M2.5 4.5 6 8l3.5-3.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {mounted &&
        pos &&
        createPortal(
          <div
            ref={menuRef}
            className={`${open ? 'select-pop-in' : 'select-pop-out'} fixed z-50 flex flex-col overflow-hidden rounded-[11px] border border-line contrast:border-border-strong bg-surface shadow-card`}
            onAnimationEnd={() => {
              if (!open) setMounted(false)
            }}
            style={{
              left: pos.left,
              minWidth: pos.minWidth,
              maxHeight: pos.maxHeight,
              ...(pos.placement === 'down'
                ? { top: pos.anchorBottom + GAP }
                : { bottom: window.innerHeight - pos.anchorTop + GAP }),
            }}
          >
            {searchable && (
              <div className="p-1 pb-0">
                <input
                  ref={inputRef}
                  value={query}
                  onChange={(e) => {
                    scrollIntentRef.current = 'nearest'
                    setQuery(e.target.value)
                    setHighlight(0)
                  }}
                  onKeyDown={onNavKey}
                  placeholder="搜索…"
                  aria-label="搜索选项"
                  aria-activedescendant={activeDesc}
                  className="w-full rounded-[7px] border border-ink-3/25 contrast:border-border-strong bg-bg-subtle px-2.5 py-1.5 text-[14px] outline-none focus:border-accent"
                />
              </div>
            )}
            <ul id={listId} role="listbox" className="min-h-0 flex-1 overflow-y-auto p-1">
              {filtered.length === 0 && (
                <li className="px-2.5 py-2 text-[14px] text-ink-3">无匹配</li>
              )}
              {filtered.map((o, i) => {
                const isSelected = o.value === value
                const isActive = i === highlight
                return (
                  <li
                    key={i}
                    id={`${listId}-opt-${i}`}
                    role="option"
                    aria-selected={isSelected}
                    onMouseEnter={() => {
                      scrollIntentRef.current = null
                      setHighlight(i)
                    }}
                    onClick={() => choose(o)}
                    className={`flex cursor-pointer items-start gap-2 rounded-[7px] px-2.5 py-2 text-[14px] ${
                      isActive ? 'bg-bg-subtle' : ''
                    } ${isSelected ? 'font-[550] text-ink-1' : 'text-ink-2'}`}
                  >
                    <svg
                      viewBox="0 0 12 12"
                      aria-hidden
                      className={`h-3.5 w-3.5 flex-none text-accent ${isSelected ? '' : 'invisible'}`}
                    >
                      <path
                        d="M2.5 6.5 5 9l4.5-5.5"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                    <span className="min-w-0 flex-1">
                      <OverflowText text={o.label} />
                      {o.description && (
                        <OverflowText text={o.description} className="mt-0.5 text-[13px] font-normal text-ink-3" />
                      )}
                    </span>
                    {o.hint && <span className="ml-auto flex-none text-[13px] font-normal text-ink-3">{o.hint}</span>}
                  </li>
                )
              })}
            </ul>
          </div>,
          document.body,
        )}
    </>
  )
}
