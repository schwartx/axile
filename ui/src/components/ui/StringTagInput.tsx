import { X } from 'lucide-react'
import {
  useRef,
  useState,
  type ClipboardEvent,
  type KeyboardEvent,
  type ReactNode,
} from 'react'

import {
  appendUniqueStrings,
  splitStringTags,
  type StringTagMode,
} from '@/components/ui/stringList'

/** 平面设置页的通用字符串标签输入器。 */
export function StringTagInput({
  id,
  value,
  mode,
  placeholder,
  help,
  action,
  onChange,
}: {
  id: string
  value: string[]
  mode: StringTagMode
  placeholder: string
  help: string
  action?: ReactNode
  onChange: (value: string[]) => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [text, setText] = useState('')
  const [armed, setArmed] = useState<string | null>(null)

  const commit = (raw: string) => {
    const incoming = splitStringTags(raw, mode)
    if (!incoming.length) return
    onChange(appendUniqueStrings(value, incoming))
    setText('')
    setArmed(null)
  }

  const remove = (item: string) => {
    onChange(value.filter((valueItem) => valueItem !== item))
    setArmed(null)
    inputRef.current?.focus()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return
    const commitsModule =
      mode === 'module' && [',', '，', ' ', 'Tab'].includes(event.key)
    if (event.key === 'Enter' || commitsModule) {
      if (event.key === 'Tab' && !text.trim()) return
      event.preventDefault()
      commit(text)
      return
    }
    if (event.key === 'Backspace' && !text && value.length) {
      const last = value[value.length - 1]
      if (armed === last) remove(last)
      else setArmed(last)
      return
    }
    setArmed(null)
  }

  const onPaste = (event: ClipboardEvent<HTMLInputElement>) => {
    const pasted = event.clipboardData.getData('text')
    const items = splitStringTags(pasted, mode)
    if (items.length <= 1) return
    event.preventDefault()
    commit(pasted)
  }

  return (
    <div>
      <div
        className="flex min-h-[88px] cursor-text flex-wrap content-start items-center gap-1.5 rounded-[9px] border border-ink-3/25 bg-surface px-3 py-2.5 focus-within:border-accent"
        onClick={() => inputRef.current?.focus()}
      >
        {value.map((item) => (
          <span
            key={item}
            className={`inline-flex h-7 max-w-full items-center gap-1 rounded-[7px] border border-line bg-fill pl-2 font-mono text-[13px] text-ink-2 ${armed === item ? 'outline-2 outline-offset-1 outline-accent' : ''}`}
          >
            <span className="truncate" title={item}>
              {item}
            </span>
            <button
              type="button"
              className="grid h-7 w-7 flex-none cursor-pointer place-items-center rounded-r-[6px] text-ink-3 hover:text-ink-1"
              aria-label={`删除 ${item}`}
              onMouseDown={(event) => event.preventDefault()}
              onClick={(event) => {
                event.stopPropagation()
                remove(item)
              }}
            >
              <X size={13} />
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          id={id}
          className="h-7 min-w-[min(16rem,100%)] flex-1 bg-transparent font-mono text-[14px] text-ink-1 outline-none placeholder:text-ink-3"
          value={text}
          placeholder={value.length ? '继续输入…' : placeholder}
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          aria-describedby={`${id}-help`}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          onBlur={() => {
            if (text.trim()) commit(text)
            setArmed(null)
          }}
        />
      </div>
      <div className="mt-1.5 flex min-h-8 flex-wrap items-start gap-3">
        <span
          id={`${id}-help`}
          className="min-w-0 flex-1 text-[12px] leading-5 text-ink-3"
        >
          {help}
        </span>
        {action}
      </div>
    </div>
  )
}
