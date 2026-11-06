import { useLayoutEffect, useRef, type ReactElement, type ReactNode } from 'react'

/** 分段控件的单个选项：值与显示文案分离。 */
export interface SegmentedOption<T extends string> {
  value: T
  label: string
  icon?: ReactNode
  badge?: ReactNode
  disabled?: boolean
}

/** 尺寸档：`md` 用于表单/配置页，`sm` 用于卡片角落等紧凑处。 */
type SegmentedSize = 'sm' | 'md'

/** 各尺寸的凹槽 / 滑块 / 按钮类片段。 */
const SIZE_CLASS: Record<SegmentedSize, { track: string; thumb: string; button: string }> = {
  md: { track: 'gap-1 rounded-[11px] p-1', thumb: 'top-1 bottom-1 rounded-lg', button: 'rounded-lg px-4 py-2 text-[15px]' },
  sm: { track: 'gap-0.5 rounded-[9px] p-0.5', thumb: 'top-0.5 bottom-0.5 rounded-md', button: 'rounded-md px-2.5 py-1 text-xs' },
}

/**
 * 「凹槽 + 滑块」分段控件（受控）。
 *
 * 高亮块是一个独立的 absolute 滑块，切换时平滑滑动，宽度随选中项文案变化。
 * 通过测量选中 button 的 offsetLeft / offsetWidth 定位滑块，配合 transform / width
 * 过渡实现动画。首帧直接就位、不做滑入；尊重系统「减少动态效果」。
 *
 * 测量结果**直写 DOM style、不走 setState**：滑块位置是布局的纯函数，回流期间
 * 布局值可能来回震荡（滚动条出现/消失、字体换宽），setState 会把每次测量都变成
 * 一次同步重渲染，极端时序下自激成「layout effect → setState → 渲染 → 再测量」
 * 的嵌套更新风暴（实测触达过 React 50 层上限）。直接改 style 则只触发 CSS 过渡、
 * 不产生 React 更新，构造上杜绝该风暴。
 *
 * @typeParam T - 选项值的字面量联合类型。
 */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  trailing,
  size = 'md',
  className = '',
  ariaLabel,
  panelId,
}: {
  value: T
  options: SegmentedOption<T>[]
  onChange: (value: T) => void
  trailing?: ReactNode
  size?: SegmentedSize
  className?: string
  ariaLabel?: string
  /** 关联面板时使用 tab 语义与方向键导航；面板由调用方渲染。 */
  panelId?: string
}): ReactElement {
  const sz = SIZE_CLASS[size]
  const trackRef = useRef<HTMLDivElement>(null)
  const thumbRef = useRef<HTMLSpanElement>(null)
  const btnRefs = useRef<(HTMLButtonElement | null)[]>([])
  const mountedRef = useRef(false)

  const activeIndex = Math.max(
    0,
    options.findIndex((o) => o.value === value),
  )

  useLayoutEffect(() => {
    const measure = () => {
      const el = btnRefs.current[activeIndex]
      const thumb = thumbRef.current
      if (!el || !thumb) return
      thumb.style.transform = `translateX(${el.offsetLeft}px)`
      thumb.style.width = `${el.offsetWidth}px`
    }
    measure()
    mountedRef.current = true

    const track = trackRef.current
    if (!track) return
    const ro = new ResizeObserver(measure)
    ro.observe(track)
    return () => ro.disconnect()
  }, [activeIndex])

  return (
    <div
      ref={trackRef}
      role={panelId ? 'tablist' : undefined}
      aria-label={ariaLabel}
      className={`relative inline-flex bg-fill ${sz.track} ${className}`}
    >
      <span
        ref={thumbRef}
        aria-hidden
        className={`absolute left-0 z-0 bg-surface shadow-sm ${sz.thumb} ${
          mountedRef.current
            ? 'transition-[transform,width] duration-200 ease-[cubic-bezier(.32,.72,0,1)] motion-reduce:transition-none'
            : ''
        }`}
      />
      {options.map((o, i) => (
        <button
          key={o.value}
          ref={(el) => {
            btnRefs.current[i] = el
          }}
          type="button"
          id={panelId ? `${panelId}-${o.value}` : undefined}
          role={panelId ? 'tab' : undefined}
          disabled={o.disabled}
          aria-pressed={panelId ? undefined : o.value === value}
          aria-selected={panelId ? o.value === value : undefined}
          aria-controls={panelId}
          tabIndex={panelId ? (o.value === value ? 0 : -1) : undefined}
          className={`relative z-10 inline-flex items-center justify-center gap-1.5 cursor-pointer transition-colors disabled:cursor-default disabled:opacity-40 ${sz.button} ${
            o.value === value ? 'font-semibold text-ink-1' : 'text-ink-2'
          }`}
          onClick={() => onChange(o.value)}
          onKeyDown={(event) => {
            if (!panelId) return
            const enabled = options.map((option, index) => option.disabled ? -1 : index).filter((index) => index >= 0)
            const current = enabled.indexOf(i)
            let next: number | undefined
            if (event.key === 'ArrowRight') next = enabled[(current + 1) % enabled.length]
            else if (event.key === 'ArrowLeft') next = enabled[(current + enabled.length - 1) % enabled.length]
            else if (event.key === 'Home') next = enabled[0]
            else if (event.key === 'End') next = enabled.at(-1)
            else return
            event.preventDefault()
            if (next === undefined) return
            onChange(options[next]!.value)
            btnRefs.current[next]?.focus()
          }}
        >
          {o.icon}{o.label}{o.badge}
        </button>
      ))}
      {trailing && <div className="relative z-10 ml-0.5 flex items-center border-l border-line/70 pl-0.5">{trailing}</div>}
    </div>
  )
}
