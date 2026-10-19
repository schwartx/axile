import { X } from 'lucide-react'
import { useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react'

import { appendUniqueSymbols, splitSymbols } from '@/components/ui/symbolTags'

export type SymbolTagVariant = 'forbidden' | 'risk'

interface SymbolTagInputProps {
  id: string
  value: string[]
  otherValue: string[]
  variant: SymbolTagVariant
  placeholder: string
  otherLabel: string
  onChange: (value: string[]) => void
  onMoveFromOther: (symbols: string[]) => void
}

/** 可连续录入、批量粘贴与快速删除的品种标签输入器。 */
export function SymbolTagInput({
  id,
  value,
  otherValue,
  variant,
  placeholder,
  otherLabel,
  onChange,
  onMoveFromOther,
}: SymbolTagInputProps) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [text, setText] = useState('')
  const [armed, setArmed] = useState<string | null>(null)
  const [conflicts, setConflicts] = useState<string[]>([])

  const tagClass =
    variant === 'forbidden'
      ? 'border-warn/45 bg-warn-tint text-warn'
      : 'border-line bg-fill text-ink-2'

  const commit = (raw: string) => {
    const incoming = splitSymbols(raw)
    if (!incoming.length) return
    const other = new Set(otherValue)
    const nextConflicts = incoming.filter((symbol) => other.has(symbol))
    const additions = incoming.filter((symbol) => !other.has(symbol))
    onChange(appendUniqueSymbols(value, additions))
    setConflicts(appendUniqueSymbols([], nextConflicts))
    setText('')
    setArmed(null)
  }

  const remove = (symbol: string) => {
    onChange(value.filter((item) => item !== symbol))
    setArmed(null)
    inputRef.current?.focus()
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return
    if (event.key === 'Tab') {
      event.preventDefault()
      if (text.trim()) commit(text)
      return
    }
    if (event.key === 'Enter' || event.key === ',' || event.key === '，' || event.key === ' ') {
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

  const handlePaste = (event: ClipboardEvent<HTMLInputElement>) => {
    event.preventDefault()
    commit(event.clipboardData.getData('text'))
  }

  return (
    <div>
      <div
        className="flex min-h-[88px] cursor-text flex-wrap content-start items-center gap-1.5 rounded-[9px] border border-ink-3/25 contrast:border-border-strong bg-surface px-3 py-2.5 focus-within:border-accent"
        onClick={() => inputRef.current?.focus()}
      >
        {value.map((symbol) => (
          <span
            key={symbol}
            className={`inline-flex h-7 items-center gap-1 rounded-[7px] border pl-2 font-mono text-[13px] ${tagClass} ${armed === symbol ? 'outline-2 outline-offset-1 outline-accent' : ''}`}
          >
            {symbol}
            <button
              type="button"
              className="grid h-7 w-7 cursor-pointer place-items-center rounded-r-[6px] text-current opacity-65 hover:opacity-100 focus-visible:outline-2 focus-visible:outline-accent"
              aria-label={`删除 ${symbol}`}
              onMouseDown={(event) => event.preventDefault()}
              onClick={(event) => {
                event.stopPropagation()
                remove(symbol)
              }}
            >
              <X size={13} />
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          id={id}
          className="h-7 min-w-[18rem] flex-1 bg-transparent font-mono text-[14px] text-ink-1 outline-none placeholder:text-ink-3"
          value={text}
          placeholder={value.length ? '继续输入…' : placeholder}
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          aria-describedby={`${id}-help`}
          onChange={(event) => {
            const next = event.target.value
            if (/[\n,，\s]/.test(next)) commit(next)
            else setText(next)
            setArmed(null)
          }}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onBlur={() => {
            if (text.trim()) commit(text)
            setArmed(null)
          }}
        />
      </div>
      {/* 仅承载冲突提示（aria-live）；录入规则说明由调用方在整个分组级别给一次。 */}
      <div id={`${id}-help`} className="mt-1 text-[12px] text-ink-3" aria-live="polite">
        {conflicts.length ? (
          <span className="text-warn">
            {conflicts.join('、')} 已在「{otherLabel}」
            <button
              type="button"
              className="ml-2 cursor-pointer text-warn underline decoration-warn/40 underline-offset-2 hover:decoration-warn"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                onMoveFromOther(conflicts)
                setConflicts([])
                inputRef.current?.focus()
              }}
            >
              移至此组
            </button>
          </span>
        ) : null}
      </div>
    </div>
  )
}
