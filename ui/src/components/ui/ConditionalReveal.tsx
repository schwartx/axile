import { useRef, type ReactNode } from 'react'

export interface ConditionalRevealOption<T extends string = string> {
  value: T
  label: string
  description?: string
}

/**
 * 互斥选择与专属参数区的通用条件展开控件。
 *
 * 所有参数区始终挂载，通过布局流收放；折叠区不可聚焦。选项使用 radio
 * 语义，并支持方向键、Home 与 End 导航。
 */
export function ConditionalReveal<T extends string>({
  label,
  help,
  value,
  options,
  error,
  onChange,
  renderPanel,
}: {
  label: string
  help?: string
  value: T
  options: ConditionalRevealOption<T>[]
  error?: string
  onChange: (value: T) => void
  renderPanel: (optionValue: T) => ReactNode
}) {
  const buttonRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const selectedIndex = options.findIndex((option) => option.value === value)

  const moveSelection = (nextIndex: number) => {
    const option = options[nextIndex]
    if (!option) return
    onChange(option.value)
    buttonRefs.current[option.value]?.focus()
  }

  return (
    <fieldset>
      <legend className="text-[14px] text-ink-2">选择一种{label}</legend>
      {help && <p className="mt-1 text-[13px] text-ink-3">{help}</p>}
      <div role="radiogroup" aria-label={label} className="mt-3 divide-y divide-line border-y border-line">
        {options.map((option, index) => {
          const selected = option.value === value
          return (
            <div
              key={option.value}
              className={`border-l-[3px] pl-3 transition-colors duration-200 motion-reduce:transition-none ${
                selected ? 'border-accent' : 'border-transparent'
              }`}
            >
              <button
                ref={(node) => { buttonRefs.current[option.value] = node }}
                type="button"
                role="radio"
                aria-checked={selected}
                tabIndex={selected || (selectedIndex === -1 && index === 0) ? 0 : -1}
                className="group -ml-2 flex w-[calc(100%+0.5rem)] cursor-pointer items-start rounded-lg bg-transparent px-2 py-3.5 text-left transition-colors duration-200 hover:bg-fill focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent motion-reduce:transition-none"
                onClick={() => onChange(option.value)}
                onKeyDown={(event) => {
                  let nextIndex: number | null = null
                  if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
                    nextIndex = (index - 1 + options.length) % options.length
                  }
                  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
                    nextIndex = (index + 1) % options.length
                  }
                  if (event.key === 'Home') nextIndex = 0
                  if (event.key === 'End') nextIndex = options.length - 1
                  if (nextIndex === null) return
                  event.preventDefault()
                  moveSelection(nextIndex)
                }}
              >
                <span>
                  <span
                    className={`block text-[17px] font-[620] transition-colors duration-200 motion-reduce:transition-none ${
                      selected ? 'text-accent' : 'text-ink-1'
                    }`}
                  >
                    {option.label}
                  </span>
                  {option.description && (
                    <span className="mt-0.5 block text-[14px] leading-relaxed text-ink-2">
                      {option.description}
                    </span>
                  )}
                </span>
              </button>
              <div
                inert={!selected}
                className={`grid transition-[grid-template-rows] duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${
                  selected ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'
                }`}
              >
                <div className="min-h-0 overflow-hidden">
                  <div className="space-y-3 pb-4">{renderPanel(option.value)}</div>
                </div>
              </div>
            </div>
          )
        })}
      </div>
      {error && <div className="mt-1 text-[13px] text-warn">{error}</div>}
    </fieldset>
  )
}
