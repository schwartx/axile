import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Box, Braces, ChevronDown, ChevronRight, Circle, Code2, Variable } from 'lucide-react'
import { SymbolKind, type DocumentSymbol, type SymbolInformation } from 'vscode-languageserver-protocol'

import type { DocumentSymbols } from '@/components/ui/pythonLanguageService'

type SymbolItem = DocumentSymbol | SymbolInformation

function start(item: SymbolItem) {
  return 'location' in item ? item.location.range.start : item.selectionRange.start
}

// oxlint-disable-next-line react/only-export-components -- 符号树与其节点标识共用同一实现。
export function symbolKey(item: SymbolItem) {
  const position = start(item)
  return `${position.line}:${position.character}:${item.kind}:${item.name}`
}

// oxlint-disable-next-line react/only-export-components -- 面包屑与符号树共用路径判定。
export function symbolPath(symbols: DocumentSymbols | null, line: number, character: number): string[] {
  if (!symbols?.length || 'location' in symbols[0]) return []
  const path: string[] = []
  let siblings = symbols as DocumentSymbol[]
  while (siblings.length) {
    const found = siblings.find(({ range }) =>
      (range.start.line < line || range.start.line === line && range.start.character <= character)
      && (range.end.line > line || range.end.line === line && range.end.character >= character))
    if (!found) break
    path.push(symbolKey(found))
    siblings = found.children ?? []
  }
  return path
}

function SymbolIcon({ kind }: { kind: number }) {
  const Icon = kind === SymbolKind.Class || kind === SymbolKind.Struct ? Box
    : kind === SymbolKind.Function || kind === SymbolKind.Method || kind === SymbolKind.Constructor ? Code2
      : kind === SymbolKind.Variable || kind === SymbolKind.Constant || kind === SymbolKind.Field || kind === SymbolKind.Property ? Variable
        : kind === SymbolKind.Module || kind === SymbolKind.Namespace || kind === SymbolKind.Package ? Braces : Circle
  return <Icon size={15} strokeWidth={1.7} aria-hidden="true" className="shrink-0 text-ink-3" />
}

export function PythonSymbolTree({ symbols, activePath, onJump }: {
  symbols: DocumentSymbols | null
  activePath: string[]
  onJump: (item: SymbolItem) => void
}) {
  const [expanded, setExpanded] = useState(() => new Set(activePath))
  const treeRef = useRef<HTMLDivElement>(null)
  const activeKey = activePath.at(-1)

  useEffect(() => {
    const target = treeRef.current?.querySelector<HTMLButtonElement>('[data-active="true"]')
      ?? treeRef.current?.querySelector<HTMLButtonElement>('[data-symbol]')
    target?.focus()
  }, [])

  const toggle = (key: string) => setExpanded((previous) => {
    const next = new Set(previous)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[data-symbol]')]
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      buttons[(index + buttons.length + (event.key === 'ArrowDown' ? 1 : -1)) % buttons.length]?.focus()
    }
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      const button = buttons[index]
      if (!button) return
      const key = button.dataset.key
      const hasChildren = button.dataset.children === 'true'
      if (!key || !hasChildren) return
      event.preventDefault()
      if (event.key === 'ArrowRight' && !expanded.has(key) || event.key === 'ArrowLeft' && expanded.has(key)) toggle(key)
    }
  }

  const render = (items: SymbolItem[], depth: number) => items.map((item) => {
    const key = symbolKey(item)
    const children = 'children' in item ? item.children ?? [] : []
    const hasChildren = children.length > 0
    const open = expanded.has(key)
    return <div key={key} role="treeitem" aria-expanded={hasChildren ? open : undefined} aria-current={key === activeKey ? 'location' : undefined}>
      <div className="group flex min-w-0 items-center rounded hover:bg-fill" style={{ paddingLeft: depth * 16 }}>
        {hasChildren ? <button type="button" tabIndex={-1} aria-label={`${open ? '收起' : '展开'} ${item.name}`} onClick={() => toggle(key)} className="grid size-6 shrink-0 place-items-center text-ink-3 hover:text-ink-1">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button>
          : <span className="size-6 shrink-0" />}
        <button type="button" data-symbol data-key={key} data-children={hasChildren} data-active={key === activeKey} title={item.name} onClick={() => onJump(item)} className={`flex min-w-0 flex-1 items-center gap-2 rounded py-1.5 pr-2 text-left focus-visible:outline-2 focus-visible:outline-accent ${key === activeKey ? 'text-accent' : ''}`}>
          <SymbolIcon kind={item.kind} /><span className={`min-w-0 flex-1 truncate ${key === activeKey ? 'font-semibold' : ''}`}>{item.name}</span><span className="shrink-0 text-ink-3">{start(item).line + 1}</span>
        </button>
      </div>
      {hasChildren && open && <div role="group">{render(children, depth + 1)}</div>}
    </div>
  })

  if (!symbols?.length) return <span className="block p-2 text-ink-3">{symbols === null ? '正在获取符号…' : '暂无符号'}</span>
  return <div ref={treeRef} role="tree" aria-label="文档符号" onKeyDown={onKeyDown}>{render(symbols, 0)}</div>
}
