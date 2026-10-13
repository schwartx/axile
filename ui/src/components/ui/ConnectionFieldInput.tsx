import { ClipboardPaste, Eye, EyeOff, FolderOpen, X } from 'lucide-react'
import {
  forwardRef,
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type CSSProperties,
  type KeyboardEvent,
  type RefObject,
} from 'react'

import type { ConnectionFieldKind } from '@/components/ui/connectionFieldValue'

interface Props {
  id: string
  label: string
  value: string
  kind: ConnectionFieldKind
  placeholder?: string
  required: boolean
  help?: string
  message?: string | null
  pasteLabel: string
  popupOpen: boolean
  reading: boolean
  showReadingFeedback: boolean
  anchorRef: RefObject<HTMLButtonElement | null>
  onChange: (value: string) => void
  onBlur?: (value: string) => void
  onBrowse?: () => void
  onPaste: (raw: string) => void
  onReadClipboard: () => void
  onKeyDown: (event: KeyboardEvent<HTMLInputElement>) => void
}

/** 连接字段的输入控件；不持有候选解析或弹层状态。 */
export const ConnectionFieldInput = forwardRef<HTMLInputElement, Props>(function ConnectionFieldInput(
  {
    id,
    label,
    value,
    kind,
    placeholder,
    required,
    help,
    message,
    pasteLabel,
    popupOpen,
    reading,
    showReadingFeedback,
    anchorRef,
    onChange,
    onBlur,
    onBrowse,
    onPaste,
    onReadClipboard,
    onKeyDown,
  },
  forwardedRef,
) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [revealed, setRevealed] = useState(false)
  const secret = kind === 'secret'
  const mono = kind === 'identifier' || secret || kind === 'endpoint' || kind === 'directory'
  const maskStyle = secret && !revealed
    ? ({ WebkitTextSecurity: 'disc' } as CSSProperties)
    : undefined

  const setRef = (node: HTMLInputElement | null) => {
    inputRef.current = node
    if (typeof forwardedRef === 'function') forwardedRef(node)
    else if (forwardedRef) forwardedRef.current = node
  }

  useEffect(() => {
    const remask = () => setRevealed(false)
    const visibility = () => document.hidden && remask()
    window.addEventListener('blur', remask)
    document.addEventListener('visibilitychange', visibility)
    return () => {
      window.removeEventListener('blur', remask)
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [])

  const handlePaste = (event: ClipboardEvent<HTMLInputElement>) => {
    event.preventDefault()
    onPaste(event.clipboardData.getData('text'))
  }

  return (
    <div className="group min-w-0">
      <div
        className={`rounded-[8px] border bg-surface px-3.5 pt-2 pb-2 transition-colors duration-150 focus-within:border-accent ${message ? 'border-warn' : 'border-line'}`}
        onClick={() => inputRef.current?.focus()}
      >
        <label htmlFor={id} className="block text-[13px] leading-4 text-ink-2">
          {label}{required ? <span className="ml-0.5 text-ink-3">*</span> : null}
        </label>
        <div className="mt-0.5 flex min-h-8 min-w-0 items-center gap-1">
          <input
            ref={setRef}
            id={id}
            type="text"
            value={value}
            placeholder={placeholder}
            inputMode={kind === 'money' ? 'decimal' : 'text'}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="none"
            spellCheck={false}
            data-1p-ignore="true"
            data-lpignore="true"
            data-bwignore="true"
            aria-invalid={message ? true : undefined}
            aria-describedby={`${id}-keys${message || help ? ` ${id}-message` : ''}`}
            aria-keyshortcuts="Alt+ArrowDown"
            className={`min-w-0 flex-1 bg-transparent text-[16px] leading-7 outline-none placeholder:text-ink-3 ${mono ? 'font-mono' : ''}`}
            style={maskStyle}
            onChange={(event) => onChange(event.target.value)}
            onPaste={handlePaste}
            onKeyDown={(event) => {
              if (event.altKey && event.key === 'ArrowDown') {
                event.preventDefault()
                event.currentTarget.parentElement?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
                return
              }
              onKeyDown(event)
            }}
            onBlur={(event) => {
              setRevealed(false)
              onBlur?.(event.currentTarget.value)
            }}
          />
          <div className="flex h-8 flex-none items-center" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault()
              inputRef.current?.focus()
              return
            }
            if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
            event.preventDefault()
            const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'))
            const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
            buttons[(index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length]?.focus()
          }}>
            {onBrowse && (
              <button tabIndex={-1} type="button" title="选择文件夹" aria-label="选择文件夹" className="grid h-8 w-8 cursor-pointer place-items-center text-ink-2 hover:text-ink-1" onMouseDown={(event) => event.preventDefault()} onClick={onBrowse}>
                <FolderOpen size={16} />
              </button>
            )}
            <button
              ref={anchorRef}
              tabIndex={-1}
              type="button"
              title={pasteLabel}
              aria-label={pasteLabel}
              aria-haspopup="dialog"
              aria-expanded={popupOpen}
              aria-controls={popupOpen ? `${id}-paste-candidates` : undefined}
              aria-busy={reading}
              disabled={reading}
              className="grid h-8 w-8 cursor-pointer place-items-center text-ink-3 transition-colors duration-150 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent disabled:cursor-default"
              onMouseDown={(event) => event.preventDefault()}
              onClick={onReadClipboard}
            >
              <ClipboardPaste size={16} className={showReadingFeedback ? 'animate-pulse text-ink-2 motion-reduce:animate-none' : undefined} />
            </button>
            {secret && (
              <button tabIndex={-1} type="button" title={revealed ? '隐藏内容' : '显示内容'} aria-label={revealed ? '隐藏内容' : '显示内容'} className="grid h-8 w-8 cursor-pointer place-items-center text-ink-2 hover:text-ink-1" onMouseDown={(event) => event.preventDefault()} onClick={() => setRevealed((current) => !current)}>
                {revealed ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            )}
            {value && (
              <button tabIndex={-1} type="button" title="清空" aria-label={`清空${label}`} className="grid h-8 w-8 cursor-pointer place-items-center text-ink-3 hover:text-ink-1" onMouseDown={(event) => event.preventDefault()} onClick={() => onChange('')}>
                <X size={15} />
              </button>
            )}
          </div>
        </div>
      </div>
      <span id={`${id}-keys`} className="sr-only">按 Alt 加向下箭头进入字段操作，左右箭头切换操作，Escape 返回输入框。</span>
      {(message || help) && (
        <div id={`${id}-message`} className={`mt-1 min-h-4 text-[13px] ${message ? 'text-warn' : 'text-ink-3'}`}>
          {message ?? help}
        </div>
      )}
    </div>
  )
})
