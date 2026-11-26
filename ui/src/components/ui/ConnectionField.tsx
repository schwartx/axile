import { forwardRef, useCallback, useId, useRef, type KeyboardEvent } from 'react'

import { ConnectionFieldCandidates } from '@/components/ui/ConnectionFieldCandidates'
import { ConnectionFieldInput } from '@/components/ui/ConnectionFieldInput'
import type { ClipboardCandidate, ClipboardParseContext } from '@/components/ui/connectionFieldClipboard'
import { connectionValueError, type ConnectionFieldKind } from '@/components/ui/connectionFieldValue'
import { useConnectionFieldClipboard } from '@/components/ui/useConnectionFieldClipboard'
import type { ChannelAccountFieldClipboard, ChannelAccountFieldConstraints } from '@/types/api'

export interface ConnectionFieldProps {
  label: string
  value: string
  kind: ConnectionFieldKind
  placeholder?: string
  fillablePlaceholder?: boolean
  required?: boolean
  help?: string
  valueHint?: string
  error?: string | null
  clipboard?: ChannelAccountFieldClipboard | null
  constraints?: ChannelAccountFieldConstraints | null
  onCandidateCommit?: (candidate: ClipboardCandidate) => void | boolean | Promise<void | boolean>
  onPasteBatchMatch?: (candidates: ClipboardCandidate[]) => void | boolean | Promise<void | boolean>
  onChange: (value: string) => void
  onBlur?: (value: string) => void
  onNavigate?: (direction: 1 | -1) => void
  onBrowse?: () => void
}

function clipboardAction(kind: ConnectionFieldKind, label: string): string {
  if (kind === 'endpoint') return '粘贴地址'
  if (kind === 'directory') return '粘贴终端路径'
  if (kind === 'money') return '粘贴金额'
  return `粘贴${label}`
}

/** 逐项连接字段：组合输入控件、剪贴板状态和候选弹层。 */
export const ConnectionField = forwardRef<HTMLInputElement, ConnectionFieldProps>(function ConnectionField(
  {
    label,
    value,
    kind,
    placeholder,
    fillablePlaceholder,
    required = false,
    help,
    valueHint,
    error,
    clipboard,
    constraints,
    onCandidateCommit,
    onPasteBatchMatch,
    onChange,
    onBlur,
    onNavigate,
    onBrowse,
  },
  forwardedRef,
) {
  const id = useId()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const anchorRef = useRef<HTMLButtonElement | null>(null)
  const context: ClipboardParseContext = { kind, fieldLabel: label, placeholder, clipboard, constraints }
  const suggestion = placeholder && (fillablePlaceholder || (
    (kind === 'endpoint' || kind === 'directory')
    && !connectionValueError({ kind, value: placeholder, label, constraints, required: true })
  )) ? placeholder : undefined
  const {
    activeCandidate,
    candidates,
    clearPasteSession,
    commitCandidate,
    committingId,
    paste,
    pasteError,
    popupOpen,
    readClipboard,
    reading,
    setActiveCandidate,
    setPasteError,
    showReadingFeedback,
  } = useConnectionFieldClipboard({ context, onChange, onCandidateCommit })
  const mono = kind === 'identifier' || kind === 'secret' || kind === 'endpoint' || kind === 'directory'

  const setRef = (node: HTMLInputElement | null) => {
    inputRef.current = node
    if (typeof forwardedRef === 'function') forwardedRef(node)
    else if (forwardedRef) forwardedRef.current = node
  }

  const closePopup = useCallback(() => {
    clearPasteSession()
    inputRef.current?.focus()
  }, [clearPasteSession])

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (popupOpen) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        const delta = event.key === 'ArrowDown' ? 1 : -1
        setActiveCandidate((activeCandidate + delta + candidates.length) % candidates.length)
        return
      }
      if (event.key === 'Enter') {
        event.preventDefault()
        const candidate = candidates[activeCandidate] ?? candidates[0]
        if (candidate) void commitCandidate(candidate).finally(() => inputRef.current?.focus())
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        closePopup()
        return
      }
    }
    if (event.key !== 'Enter' || !onNavigate) return
    event.preventDefault()
    onNavigate(event.shiftKey ? -1 : 1)
  }

  const fillMatched = async () => {
    if (!onPasteBatchMatch) return
    try {
      const result = await onPasteBatchMatch(candidates)
      if (result === false) return
      setPasteError(null)
      clearPasteSession(false)
      inputRef.current?.focus()
    } catch (caught) {
      setPasteError(caught instanceof Error && caught.message ? caught.message : '暂时无法填入匹配地址')
    }
  }

  const message = error ?? pasteError
  return (
    <>
      <ConnectionFieldInput
        ref={setRef}
        id={id}
        label={label}
        value={value}
        kind={kind}
        placeholder={placeholder}
        suggestion={suggestion}
        required={required}
        help={help}
        valueHint={valueHint}
        message={message}
        pasteLabel={clipboardAction(kind, label)}
        popupOpen={popupOpen}
        reading={reading}
        showReadingFeedback={showReadingFeedback}
        anchorRef={anchorRef}
        onChange={(next) => {
          setPasteError(null)
          clearPasteSession()
          onChange(next)
        }}
        onBlur={onBlur}
        onBrowse={onBrowse}
        onPaste={paste}
        onReadClipboard={() => void readClipboard()}
        onKeyDown={handleKeyDown}
      />
      {popupOpen && (
        <ConnectionFieldCandidates
          id={id}
          label={label}
          value={value}
          mono={mono}
          candidates={candidates}
          activeCandidate={activeCandidate}
          committingId={committingId}
          pasteError={pasteError}
          anchorRef={anchorRef}
          onActiveCandidate={setActiveCandidate}
          onCommit={(candidate) => void commitCandidate(candidate).finally(() => inputRef.current?.focus())}
          onClose={closePopup}
          onFillMatched={onPasteBatchMatch ? () => void fillMatched() : undefined}
        />
      )}
    </>
  )
})
