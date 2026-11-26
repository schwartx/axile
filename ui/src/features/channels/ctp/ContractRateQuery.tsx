import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'

import { InputHint } from '@/components/ui/InputHint'
import { apiGet } from '@/lib/api/client'
import type { AccountToolProps } from '@/features/tools/ToolsButton'

const exchanges: Record<string, string> = { SHFE: '上期所', DCE: '大商所', CZCE: '郑商所', CFFEX: '中金所', INE: '上期能源', GFEX: '广期所' }
const validPrefix = /^[A-Za-z][A-Za-z0-9]*$/
const contract = /^[A-Za-z]+\d{3,4}$/

type Candidate = { symbol: string; exchange: string }

export function ContractRateQuery<T extends { instrument_id: string }>({
  accountId, open, label, columns, query, renderRows,
}: AccountToolProps & {
  label: string
  columns: readonly string[]
  query: (accountId: number, code: string, signal: AbortSignal) => Promise<T>
  renderRows: (rates: T) => ReactNode
}) {
  const id = useId()
  const listId = `${id}-suggestions`
  const [symbol, setSymbol] = useState('')
  const [prefix, setPrefix] = useState('')
  const [suggestions, setSuggestions] = useState<{ prefix: string; rows: Candidate[]; message?: string } | null>(null)
  const [highlight, setHighlight] = useState(-1)
  const [pendingCode, setPendingCode] = useState('')
  const [error, setError] = useState('')
  const [results, setResults] = useState(() => new Map<string, T>())
  const request = useRef<AbortController | null>(null)
  const currentSuggestions = suggestions?.prefix === prefix ? suggestions : null
  const candidates = currentSuggestions?.rows ?? []
  const expanded = open && candidates.length > 0

  useEffect(() => () => request.current?.abort(), [])
  useEffect(() => {
    if (!open || !validPrefix.test(prefix)) return
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      try {
        const reply = await apiGet<{ data: Candidate[]; cache_ready: boolean }>(`/instrument-catalog/search?${new URLSearchParams({ prefix })}`, controller.signal)
        if (controller.signal.aborted) return
        const message = reply.cache_ready ? `没有以 ${prefix} 开头的期货合约，请核对代码。` : '合约缓存尚未就绪，可输入完整代码查询。'
        setSuggestions({ prefix, rows: reply.data, message: reply.data.length ? undefined : message })
      } catch {
        if (!controller.signal.aborted) setSuggestions({ prefix, rows: [], message: '合约联想暂不可用，可输入完整代码查询。' })
      }
    }, 150)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [open, prefix])

  function dismissCandidates() {
    setPrefix('')
    setSuggestions(null)
    setHighlight(-1)
  }

  function changeSymbol(value: string) {
    dismissCandidates()
    setSymbol(value)
    setPrefix(value.trim())
    setError('')
  }

  async function submit(code: string) {
    if (request.current) return
    setError('')
    if (!contract.test(code)) {
      if (validPrefix.test(code)) {
        setPrefix(code)
        if (candidates.length) setHighlight(0)
      } else {
        setError('期货合约代码应为字母加月份，例如 rb2701')
      }
      return
    }
    dismissCandidates()
    setSymbol(code)
    const controller = new AbortController()
    request.current = controller
    setPendingCode(code)
    try {
      const rates = await query(accountId, code, controller.signal)
      if (!controller.signal.aborted) {
        setResults(previous => new Map(previous).set(rates.instrument_id.toLowerCase(), rates))
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : `${label}查询失败`)
    } finally {
      request.current = null
      if (!controller.signal.aborted) setPendingCode('')
    }
  }

  function onSearchKey(event: KeyboardEvent<HTMLInputElement>) {
    if (!expanded || event.nativeEvent.isComposing) return
    if (event.key === 'Escape') {
      event.stopPropagation()
      dismissCandidates()
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      const index = highlight < 0 && step < 0 ? candidates.length - 1 : (highlight + step + candidates.length) % candidates.length
      setHighlight(index)
      document.getElementById(`${listId}-${index}`)?.scrollIntoView({ block: 'nearest' })
    } else if (event.key === 'Enter' && highlight >= 0) {
      event.preventDefault()
      void submit(candidates[highlight].symbol)
    }
  }

  const pendingNew = pendingCode && !results.has(pendingCode.toLowerCase())
  return <>
    <form onSubmit={event => { event.preventDefault(); void submit(symbol.trim()) }} className="space-y-2">
      <div className="flex items-center gap-2 text-sm">
        <label htmlFor={id} className="shrink-0 text-ink-2">合约</label>
        <div className="relative min-w-0 flex-1">
          <input id={id} data-tool-autofocus role="combobox" aria-autocomplete="list" aria-expanded={expanded}
            aria-controls={expanded ? listId : undefined} aria-activedescendant={expanded && highlight >= 0 ? `${listId}-${highlight}` : undefined}
            value={symbol} maxLength={32} readOnly={!!pendingCode} autoComplete="off" spellCheck={false}
            onKeyDown={onSearchKey} onBlur={event => {
              if (!event.currentTarget.form?.contains(event.relatedTarget)) dismissCandidates()
            }}
            onChange={event => changeSymbol(event.target.value)}
            className="w-full min-w-0 rounded-lg border border-ink-3/30 contrast:border-border-strong bg-surface px-2.5 py-1.5 outline-none focus:border-accent" />
          <InputHint inputId={id} value={symbol} suggestion="rb2701" onFill={changeSymbol} disabled={!!pendingCode} className="left-[11px]" />
        </div>
        <button type="submit" disabled={!!pendingCode || !symbol.trim()}
          className="shrink-0 cursor-pointer rounded-lg border border-line px-2.5 py-1.5 text-ink-1 hover:bg-fill disabled:cursor-not-allowed disabled:opacity-45">
          {pendingCode ? '查询中…' : '查询'}
        </button>
      </div>
      {expanded && <ul id={listId} aria-label="合约候选" role="listbox" className="ml-9 max-h-48 overflow-y-auto rounded-lg border border-line p-1 text-sm">
        {candidates.map(({ symbol, exchange }, index) => <li key={`${exchange}.${symbol}`} id={`${listId}-${index}`}
          role="option" aria-selected={highlight === index} onMouseDown={event => event.preventDefault()}
          onClick={() => void submit(symbol)}
          className="flex cursor-pointer justify-between gap-2 rounded-md px-2 py-1.5 hover:bg-fill aria-selected:bg-fill">
          <span>{symbol}</span><span className="text-ink-2">{exchanges[exchange] ?? exchange}</span>
        </li>)}
      </ul>}
      {currentSuggestions?.message && <p role="status" className="break-words text-xs text-ink-2">{currentSuggestions.message}</p>}
      {error && <p role="alert" className="break-words text-xs text-warn">{error}</p>}
    </form>
    {(results.size > 0 || pendingNew) && <div className="mt-3 border-t border-line pt-2 text-sm" aria-live="polite">
      <table aria-label={`${label}对照`} className="w-full tabular-nums">
        <thead className="text-xs text-ink-2"><tr>
          <th scope="col" className="py-1 text-left font-normal">合约</th>
          {columns.map(column => <th key={column} scope="col" className="py-1 pl-2 text-right font-normal">{column}</th>)}
        </tr></thead>
        <tbody className="divide-y divide-line">
          {Array.from(results.values(), renderRows)}
          {pendingNew && <tr>
            <th scope="row" className="break-all py-1.5 text-left font-medium">{pendingCode}</th>
            <td colSpan={columns.length} className="py-1.5 pl-2 text-right text-xs text-ink-2">查询中…</td>
          </tr>}
        </tbody>
      </table>
    </div>}
  </>
}
