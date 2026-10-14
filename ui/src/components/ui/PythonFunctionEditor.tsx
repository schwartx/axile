import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { PythonCheckStatus } from '@/components/ui/PythonCheckStatus'
import { type PythonChecks } from '@/components/ui/pythonChecks'
import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode, type Ref } from 'react'
import { createPortal } from 'react-dom'
import { python } from '@codemirror/lang-python'
import { linter, forceLinting, forEachDiagnostic, type Diagnostic } from '@codemirror/lint'
import { Compartment, StateEffect } from '@codemirror/state'
import { undo, redo, isolateHistory } from '@codemirror/commands'
import { openSearchPanel, gotoLine } from '@codemirror/search'
import { EditorView, scrollPastEnd } from '@codemirror/view'
import CodeMirror, { type ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { Check, Clipboard, Play, TriangleAlert, X } from 'lucide-react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { connectPython, type DocumentSymbols, type LanguageStatus } from '@/components/ui/pythonLanguageService'
import type { PythonEditorKind } from '@/components/ui/pythonEditorContract'
import { OverflowText } from '@/components/ui/OverflowText'
import { LSPPlugin, type LSPClient } from '@codemirror/lsp-client'
import type { DocumentSymbol, SymbolInformation, Range } from 'vscode-languageserver-protocol'
import { PythonSaveBar } from '@/components/ui/PythonSaveBar'
import { usePythonSaveShortcut, type PythonSaveAction } from '@/components/ui/pythonSaveAction'
import { editingExtensions, isMac } from '@/components/ui/pythonEditorExtensions'
import { apiSend } from '@/lib/api/client'
import { PythonSourcePreview, type SourceTab } from '@/components/ui/PythonSourcePreview'
import { quickFix, renamePythonSymbol } from '@/components/ui/pythonLanguageFeatures'
import { jumpToDefinition, findReferences } from '@codemirror/lsp-client'
import { pythonEditorTheme } from '@/components/ui/pythonEditorTheme'
import { pythonEditorZoomKeys, usePythonEditorFontSize } from '@/components/ui/pythonEditorFontSize'
import { pythonStickyScroll } from '@/components/ui/pythonStickyScroll'
import { centeredFoldGutter, foldedLineHighlight } from '@/components/ui/pythonEditorReading'
import { PythonSymbolTree, symbolPath } from '@/components/ui/PythonSymbolTree'

export interface PythonProblem { line: number; message: string; source: string; severity: string }
const runtimeChanged = StateEffect.define<null>()
const basicSetup = { foldGutter: false, highlightActiveLine: true, highlightActiveLineGutter: true, autocompletion: true }
const toolbarActionClass = 'flex-none rounded px-2 py-1 cursor-pointer transition-colors duration-150 hover:bg-fill hover:text-ink-1 active:bg-ink-1/10 active:text-ink-1 aria-expanded:bg-fill aria-expanded:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-1 disabled:pointer-events-none disabled:opacity-40 motion-reduce:transition-none'

export interface PythonValidationState {
  valid: boolean
  errorLine?: number | null
  errorType?: string | null
  errorMessage?: string | null
  traceback?: string | null
  error?: unknown
}

/** 暴露给外部控制的编辑器句柄（如「粘贴」按钮点击后把焦点还给代码区）。 */
export interface PythonEditorHandle {
  focus: () => void
  check: () => void
  retryQuickFix: () => void
  revealLine: (line: number) => void
  replaceCode: (code: string) => void
}

export type PythonRunStatus = 'running' | 'idle' | 'stale' | 'pass' | 'fail'

/** 试跑状态归约：console 工具条与工作台结果 panel 共用同一份状态语义。 */
// oxlint-disable-next-line react/only-export-components -- 状态归约与编辑器组件同源，刻意合并
export function pythonRunStatus(
  running: boolean,
  result: PythonValidationState | null,
  stale: boolean,
): PythonRunStatus {
  return running ? 'running' : result == null ? 'idle' : stale ? 'stale' : result.valid ? 'pass' : 'fail'
}

/** 状态外观：成败不走红绿——通过/进行 = 蓝，未通过 = 琥珀，无事 = 中性。 */
// oxlint-disable-next-line react/only-export-components -- 状态外观表与状态归约同槽，刻意合并
export const PYTHON_RUN_STYLE: Record<PythonRunStatus, { rail: string; band: string; text: string; body: string }> = {
  running: { rail: 'border-accent', band: 'border-accent/30 bg-accent-soft', text: 'text-accent', body: '试跑中…' },
  idle: { rail: 'border-line', band: 'border-line bg-surface', text: 'text-ink-3', body: '尚未试跑' },
  stale: { rail: 'border-line', band: 'border-line bg-surface', text: 'text-ink-3', body: '代码已改 · 结果为上次试跑' },
  pass: { rail: 'border-accent', band: 'border-accent/30 bg-accent-soft', text: 'text-accent', body: '试跑通过' },
  fail: { rail: 'border-warn', band: 'border-warn/30 bg-warn/10', text: 'text-warn', body: '未通过' },
}

/** 结果正文（console 结果带与 PythonRunPanel 共用）：通过 → resultContent；未通过 → 错误摘要 + traceback。 */
export function PythonRunResultBody({
  result,
  stale,
  resultContent,
}: {
  result: PythonValidationState
  stale: boolean
  resultContent?: ReactNode
}) {
  return (
    <div className={stale ? 'opacity-55' : undefined}>
      {result.valid ? (
        (resultContent ?? <p className="text-[14px] text-ink-2">函数执行成功。</p>)
      ) : (
        <>
          <ErrorNotice title="Python 试跑失败" error={result.error ?? {
            message: [result.errorType, result.errorMessage].filter(Boolean).join(': ') || '执行出错', traceback: result.traceback,
          }} />
        </>
      )}
    </div>
  )
}

/**
 * 代码 + 试跑 console，两种布局：
 *
 * - `console`（默认，表单/向导场景）：工具条（状态句/操作/试跑）钉在代码上方，
 *   结果区在工具条与代码之间 grid-fr 常挂收放，代码区按 height/min/max 自定高度。
 * - `workbench`（工作台编辑页）：纯代码区吃满父容器，无任何工具条——控制件与
 *   结果呈现全部外置（结果走 :func:`PythonRunPanel`，可摆进左栏做 split pane）。
 *
 * 两种布局都支持 Ctrl/Cmd+Enter 试跑；失败时错误行进 lint 并滚入可视区。
 */
export function PythonFunctionEditor({
  kind,
  code,
  onChange,
  running,
  runDisabled = false,
  saveAction,
  result,
  onRun,
  controls,
  resultContent,
  docHref,
  workbenchTitle = '目标函数',
  height = '320px',
  minHeight,
  maxHeight,
  runLabel = '试跑',
  disabled = false,
  stale = false,
  fill = false,
  layout = 'console',
  ref,
  onProblems,
  onChecks,
  headerTarget,
  statusTarget,
}: {
  kind: PythonEditorKind
  code: string
  onChange: (code: string) => void
  running: boolean
  /** 保存等外部操作期间阻止试跑；按钮和 Ctrl/Cmd+Enter 使用同一条件。 */
  runDisabled?: boolean
  /** 可选保存契约：嵌入式编辑器也使用工作台的状态栏、还原和快捷键。 */
  saveAction?: PythonSaveAction
  result: PythonValidationState | null
  onRun: () => void
  controls?: ReactNode
  resultContent?: ReactNode
  docHref?: string
  workbenchTitle?: string
  height?: string
  minHeight?: string
  maxHeight?: string
  runLabel?: string
  disabled?: boolean
  /** 代码在最后一次试跑后又改过：结果保留展示但整体降级为中性，不冒充新结论。 */
  stale?: boolean
  /** 吃满父容器高度（父链须给出确定高度）；console 布局缺省按 height/min/max 定高。 */
  fill?: boolean
  /** 布局形态：console = 表单内嵌（工具条 + 结果带）；workbench = 纯代码区。 */
  layout?: 'console' | 'workbench'
  ref?: Ref<PythonEditorHandle>
  onProblems?: (problems: PythonProblem[]) => void
  onChecks?: (checks: PythonChecks) => void
  headerTarget?: HTMLElement | null
  statusTarget?: HTMLElement | null
}) {
  const cmRef = useRef<ReactCodeMirrorRef>(null)
  const fontSize = usePythonEditorFontSize()
  const hasCode = code.trim().length > 0
  const runAllowed = !running && !disabled && !runDisabled && hasCode
  const run = () => { if (runAllowed) onRun() }
  const onRunRef = useRef(run)
  onRunRef.current = run
  usePythonSaveShortcut(saveAction)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const changeCode = useCallback((value: string) => onChangeRef.current(value), [])
  const [view, setView] = useState<EditorView | null>(null)
  const [checks, setChecks] = useState<PythonChecks | null>(null)
  const checksCallback = useRef(onChecks)
  checksCallback.current = onChecks
  const [languageStatus, setLanguageStatus] = useState<LanguageStatus>('connecting')
  const [languageClient, setLanguageClient] = useState<LSPClient | null>(null)
  const [position, setPosition] = useState({ line: 1, column: 1 })
  const [symbols, setSymbols] = useState<DocumentSymbols | null>(null)
  const [menu, setMenu] = useState<{ kind: 'actions' | 'symbols'; x: number; y: number } | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [formatting, setFormatting] = useState(false)
  const [editorMessage, setEditorMessage] = useState('')
  const [sourceTabs, setSourceTabs] = useState<SourceTab[]>([])
  const [activeSource, setActiveSource] = useState<string | null>(null)
  const [sourceSymbols, setSourceSymbols] = useState<Record<string, DocumentSymbols | null>>({})
  const [sourcePositions, setSourcePositions] = useState<Record<string, { line: number; character: number }>>({})
  const sourceViews = useRef(new Map<string, EditorView>())
  const pendingSourceViews = useRef(new Map<string, ((view: EditorView | null) => void)[]>())
  const languageSlot = useMemo(() => new Compartment(), [])
  const runtimeRef = useRef({ result, stale })
  runtimeRef.current = { result, stale }
  const problemsRef = useRef(onProblems)
  problemsRef.current = onProblems
  const problemsKey = useRef('')
  const formatRef = useRef<() => void>(() => {})
  const formattingRef = useRef(false)

  useEffect(() => {
    if (!view) return
    return connectPython(view, languageSlot, kind, (status) => { setLanguageStatus(status); if (status !== 'ready') setSymbols(null) }, (uri, code) => new Promise((resolve) => {
      if (uri === LSPPlugin.get(view)?.uri) {
        setActiveSource(null)
        resolve(view)
        return
      }
      const existing = sourceViews.current.get(uri)
      setActiveSource(uri)
      if (existing) { resolve(existing); return }
      pendingSourceViews.current.set(uri, [...(pendingSourceViews.current.get(uri) ?? []), resolve])
      setSourceTabs((tabs) => tabs.some((tab) => tab.uri === uri) ? tabs : [...tabs, { uri, code }])
    }), (next) => setSymbols(next), setLanguageClient, (next) => { setChecks(next); checksCallback.current?.(next) })
  }, [view, languageSlot, kind])

  const closeSource = (uri: string) => {
    pendingSourceViews.current.get(uri)?.forEach((resolve) => resolve(null))
    pendingSourceViews.current.delete(uri)
    sourceViews.current.delete(uri)
    setSourceSymbols((current) => { const next = { ...current }; delete next[uri]; return next })
    setSourcePositions((current) => { const next = { ...current }; delete next[uri]; return next })
    setSourceTabs((tabs) => tabs.filter((tab) => tab.uri !== uri))
    if (activeSource === uri) {
      setActiveSource(null)
      requestAnimationFrame(() => view?.focus())
    }
  }
  const sourceName = (uri: string) => decodeURIComponent(uri.split('/').at(-1) || uri)
  const sourceTab = sourceTabs.find((tab) => tab.uri === activeSource)
  const sourcePosition = activeSource ? sourcePositions[activeSource] : undefined
  const updateSourceCursor = useCallback((uri: string, line: number, character: number) => {
    setSourcePositions((positions) => ({ ...positions, [uri]: { line, character } }))
  }, [])
  const updateSourceSymbols = useCallback((uri: string, next: DocumentSymbols | null) => {
    setSourceSymbols((current) => ({ ...current, [uri]: next }))
  }, [])

  const replaceCode = useCallback((text: string) => {
    const editor = cmRef.current?.view
    if (!editor || disabled) return
    editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: text }, annotations: isolateHistory.of('full') })
    editor.focus()
  }, [disabled])

  formatRef.current = () => {
    const editor = cmRef.current?.view
    if (!editor || disabled || formattingRef.current) return
    const before = editor.state.doc
    formattingRef.current = true
    setFormatting(true)
    setEditorMessage('')
    void apiSend<{ code: string }>('POST', '/editor/format', { code: before.toString() })
      .then(({ code: formatted }) => {
        if (cmRef.current?.view !== editor || editor.state.readOnly) return
        if (editor.state.doc !== before) {
          setEditorMessage('代码已变化，请重新格式化')
          return
        }
        if (formatted !== before.toString()) replaceCode(formatted)
      })
      .catch((error: unknown) => setEditorMessage(error instanceof Error ? error.message : '格式化失败'))
      .finally(() => { formattingRef.current = false; setFormatting(false) })
  }

  useImperativeHandle(
    ref,
    () => ({
      check: () => { const editor = cmRef.current?.view; if (editor) forceLinting(editor) },
      retryQuickFix: () => { const editor = cmRef.current?.view; if (editor) quickFix(editor, kind) },
      focus: () => cmRef.current?.view?.focus(),
      revealLine: (lineNo: number) => {
        const view = cmRef.current?.view
        if (!view) return
        const line = view.state.doc.line(Math.min(Math.max(lineNo, 1), view.state.doc.lines))
        view.dispatch({ selection: { anchor: line.from }, effects: EditorView.scrollIntoView(line.from, { y: 'center' }) })
        view.focus()
      },
      replaceCode,
    }),
    [replaceCode, kind],
  )

  useEffect(() => {
    const view = cmRef.current?.view
    if (!view) return
    view.dispatch({ effects: runtimeChanged.of(null) })
    forceLinting(view)
    if (result && !stale && !result.valid && result.errorLine != null) {
      const lineNo = Math.min(Math.max(result.errorLine, 1), view.state.doc.lines)
      const line = view.state.doc.line(lineNo)
      view.dispatch({ effects: EditorView.scrollIntoView(line.from, { y: 'center' }) })
    }
  }, [result, stale])

  const paste = async () => {
    try {
      const text = await navigator.clipboard.readText()
      if (text) replaceCode(text)
    } catch {
      setEditorMessage('无法读取剪贴板，请在编辑器内使用粘贴快捷键')
    } finally {
      cmRef.current?.view?.focus()
    }
  }

  const status = pythonRunStatus(running, result, stale)
  const style = PYTHON_RUN_STYLE[status]

  const extensions = useMemo(() => [
    pythonEditorTheme,
    pythonEditorZoomKeys,
    pythonStickyScroll,
    scrollPastEnd(),
    python(),
    centeredFoldGutter,
    foldedLineHighlight,
    languageSlot.of([]),
    editingExtensions(() => formatRef.current(), () => onRunRef.current()),
    linter((editor): Diagnostic[] => {
      const { result: current, stale: outdated } = runtimeRef.current
      if (!current || current.valid || outdated || current.errorLine == null) return []
      const line = editor.state.doc.line(Math.min(Math.max(current.errorLine, 1), editor.state.doc.lines))
      return [{ from: line.from, to: line.to, severity: 'error', source: '试跑',
        message: [current.errorType, current.errorMessage].filter(Boolean).join(': ') || '试跑未通过' }]
    }, { needsRefresh: (update) => update.transactions.some((transaction) => transaction.effects.some((effect) => effect.is(runtimeChanged))) }),
    EditorView.updateListener.of((update) => {
      if (update.selectionSet || update.docChanged) {
        const line = update.state.doc.lineAt(update.state.selection.main.head)
        setPosition({ line: line.number, column: update.state.selection.main.head - line.from + 1 })
      }
      const problems: PythonProblem[] = []
      forEachDiagnostic(update.state, (diagnostic, from) => problems.push({
        line: update.state.doc.lineAt(from).number, message: diagnostic.message,
        source: diagnostic.source ?? 'ty', severity: diagnostic.severity,
      }))
      const key = JSON.stringify(problems)
      if (key !== problemsKey.current) {
        problemsKey.current = key
        queueMicrotask(() => problemsRef.current?.(problems))
      }
    }),
  ], [languageSlot])

  const mod = isMac() ? '⌘' : 'Ctrl'
  const shortcuts: Record<string, string> = {
    '查找 / 替换': `${mod}+F`, '查找': `${mod}+F`, '格式化': 'Shift+Alt+F',
    '撤销': `${mod}+Z`, '重做': isMac() ? '⌘+Shift+Z' : 'Ctrl+Y',
    '定义': 'F12', '引用': 'Shift+F12', '重命名': 'F2', '快速修复': `${mod}+.`,
  }
  const shortcutHint = (name: string) => shortcuts[name]
    ? <span aria-hidden="true" className="shrink-0 whitespace-nowrap text-[0.9em] font-normal text-ink-3">{shortcuts[name]}</span>
    : null
  const editor = activeSource ? sourceViews.current.get(activeSource) ?? null : view
  const plugin = editor && LSPPlugin.get(editor)
  const cursor = activeSource ? sourcePositions[activeSource] ?? null : plugin && editor ? plugin.toPosition(editor.state.selection.main.head) : null
  const activeSymbols = activeSource ? sourceSymbols[activeSource] ?? null : symbols
  const contains = (range: Range) => cursor && (range.start.line < cursor.line || range.start.line === cursor.line && range.start.character <= cursor.character)
    && (range.end.line > cursor.line || range.end.line === cursor.line && range.end.character >= cursor.character)
  const flat = activeSymbols?.length && 'location' in activeSymbols[0]
  const roots = flat ? (activeSymbols as SymbolInformation[]).filter((item) => item.location.uri === plugin?.uri) : (activeSymbols as DocumentSymbol[] | null)
  const chain: DocumentSymbol[] = []
  if (roots && !flat) {
    let siblings = roots as DocumentSymbol[]
    while (siblings.length) {
      const found: DocumentSymbol | undefined = siblings.find((item) => contains(item.range))
      if (!found) break
      chain.push(found)
      siblings = found.children ?? []
    }
  }
  const jumpSymbol = (item: DocumentSymbol | SymbolInformation) => {
    if (!editor || !plugin) return
    const location = 'location' in item ? item.location.range.start : item.selectionRange.start
    const offset = plugin.fromPosition(location, editor.state.doc)
    editor.dispatch({ selection: { anchor: offset }, effects: EditorView.scrollIntoView(offset, { y: 'center' }) })
    setMenu(null)
    editor.focus()
  }
  const openMenu = (kind: 'actions' | 'symbols', target: HTMLElement) => {
    const rect = target.getBoundingClientRect()
    setMenu({ kind, x: Math.min(rect.left, window.innerWidth - 288), y: Math.min(rect.bottom + 3, window.innerHeight - 350) })
  }
  const openContextMenu = (event: MouseEvent<HTMLDivElement>, target: EditorView) => {
    event.preventDefault()
    const offset = target.posAtCoords({ x: event.clientX, y: event.clientY })
    const selection = target.state.selection.main
    if (offset != null && !(selection.from !== selection.to && offset >= selection.from && offset <= selection.to)) {
      target.dispatch({ selection: { anchor: offset } })
    }
    setMenu({ kind: 'actions', x: Math.min(event.clientX, window.innerWidth - 240), y: Math.min(event.clientY, window.innerHeight - 350) })
  }
  const command = (name: string) => {
    setMenu(null)
    if (!editor) return
    editor.focus()
    if (name === '查找 / 替换' || name === '查找') openSearchPanel(editor)
    if (editor.state.readOnly && !['查找', '定义', '引用'].includes(name)) return
    if (name === '格式化') formatRef.current()
    if (name === '粘贴') void paste()
    if (name === '撤销') undo(editor)
    if (name === '重做') redo(editor)
    if (name === '定义') jumpToDefinition(editor)
    if (name === '引用') findReferences(editor)
    if (name === '重命名') renamePythonSymbol(editor)
    if (name === '快速修复') quickFix(editor, kind)
  }
  useEffect(() => {
    if (!menu) return
    const close = (event: PointerEvent) => { if (!menuRef.current?.contains(event.target as Node)) { setMenu(null); if (!(event.target instanceof HTMLElement && event.target.closest('button, a, input, .cm-editor'))) editor?.focus() } }
    if (menu.kind === 'actions') menuRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [menu, editor])
  const actions = ['查找 / 替换', '撤销', '重做', '定义', '引用', '重命名', '快速修复']
  const sourceActions = ['查找', '定义', '引用']
  const actionDisabled = (name: string) => sourceTab ? ['定义', '引用'].includes(name) && !plugin?.client.connected
    : name === '格式化' && formatting ? true : name === '开发文档 ↗' ? false : ['定义', '引用', '重命名', '快速修复'].includes(name) ? languageStatus !== 'ready' || (disabled && ['重命名', '快速修复'].includes(name)) : disabled && name !== '查找 / 替换'
  const workbenchHeader = (
    <div className="@container flex h-8 min-w-0 items-center gap-1 border-b border-line bg-surface px-3 text-[12px] text-ink-2">
      {sourceTab ? <>
        <div className="flex min-w-0 flex-1 items-center overflow-hidden whitespace-nowrap">
          {decodeURIComponent(sourceTab.uri).split('/').filter(Boolean).slice(-3, -1).map((part) => <span key={part} className="hidden shrink-0 items-center gap-1 @min-[520px]:inline-flex"><span aria-hidden>›</span>{part}</span>)}
          <button type="button" title={decodeURIComponent(sourceTab.uri)} className="min-w-0 truncate pl-1 text-ink-1" aria-haspopup="tree" aria-expanded={menu?.kind === 'symbols'} onClick={(event) => openMenu('symbols', event.currentTarget)}>{sourceName(sourceTab.uri)}</button>
          {(chain.length > 3 ? [chain[0], null, chain.at(-1)!] : chain).map((item) => <span key={item?.name ?? 'ellipsis'} className="flex min-w-0 items-center gap-1 pl-1"><span aria-hidden>›</span><button type="button" className="min-w-0 max-w-32" aria-haspopup="tree" aria-expanded={menu?.kind === 'symbols'} onClick={(event) => openMenu('symbols', event.currentTarget)}>{item ? <OverflowText text={item.name} /> : '…'}</button></span>)}
        </div>
        <span className="shrink-0">只读</span>
        <button type="button" className={toolbarActionClass} aria-haspopup="menu" aria-expanded={menu?.kind === 'actions'} onClick={(event) => openMenu('actions', event.currentTarget)}>菜单</button>
      </> : <>
      <div className="flex min-w-0 flex-1 items-center overflow-hidden whitespace-nowrap">
        <button type="button" className="flex-none text-ink-1" aria-haspopup="tree" aria-expanded={menu?.kind === 'symbols'} onClick={(event) => openMenu('symbols', event.currentTarget)}>{workbenchTitle}</button>
        {(chain.length > 3 ? [chain[0], null, chain.at(-1)!] : chain).map((item) => <span key={item?.name ?? 'ellipsis'} className="flex min-w-0 items-center gap-1 pl-1">
          <span aria-hidden>›</span><button type="button" className="min-w-0 max-w-32" aria-haspopup="tree" aria-expanded={menu?.kind === 'symbols'} onClick={(event) => openMenu('symbols', event.currentTarget)}>{item ? <OverflowText text={item.name} /> : '…'}</button>
        </span>)}
      </div>
      <button type="button" className={`${toolbarActionClass} hidden items-center gap-1 @min-[480px]:inline-flex`} disabled={disabled} onClick={() => void paste()}><Clipboard size={12} /> 粘贴</button>
      <button type="button" className={`${toolbarActionClass} hidden items-center gap-2 @min-[480px]:inline-flex`} disabled={disabled || formatting} onClick={() => formatRef.current()}>{formatting ? '格式化中…' : '格式化'}{shortcutHint('格式化')}</button>
      <button type="button" className={toolbarActionClass} aria-haspopup="menu" aria-expanded={menu?.kind === 'actions'} onClick={(event) => openMenu('actions', event.currentTarget)}>菜单</button>
      {docHref && <a className={`${toolbarActionClass} hidden @min-[480px]:block`} href={docHref} target="_blank" rel="noopener">开发文档 ↗</a>}
      </>}
    </div>
  )
  const popup = menu && createPortal(<div ref={menuRef} tabIndex={-1} onKeyDown={(event) => {
    if (event.key === 'Escape') { setMenu(null); editor?.focus() }
    if (menu.kind === 'actions' && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault()
      const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
      buttons[(buttons.indexOf(document.activeElement as HTMLButtonElement) + buttons.length + (event.key === 'ArrowDown' ? 1 : -1)) % buttons.length]?.focus()
    }
  }} style={{ left: Math.max(8, menu.x), top: Math.max(8, menu.y) }} role={menu.kind === 'actions' ? 'menu' : undefined} className={`fixed z-[100] max-h-[65vh] overflow-auto rounded-md border border-line bg-surface p-1 text-[12px] text-ink-1 shadow-lg ${menu.kind === 'symbols' ? 'w-[min(18rem,calc(100vw-16px))]' : 'w-[min(15rem,calc(100vw-16px))]'}`}>
    {menu.kind === 'symbols' ? <PythonSymbolTree key={`${menu.x}:${menu.y}`} symbols={roots} activePath={cursor ? symbolPath(activeSymbols, cursor.line, cursor.character) : []} onJump={jumpSymbol} /> : <>{(sourceTab ? sourceActions : [...actions, '粘贴', '格式化', ...(docHref ? ['开发文档 ↗'] : [])]).map((name) => <button key={name} role="menuitem" disabled={actionDisabled(name)} className={`${toolbarActionClass} flex w-full items-center justify-between gap-4 py-1.5 text-left`} onClick={() => name === '开发文档 ↗' ? window.open(docHref!, '_blank', 'noopener') : command(name)}><span>{name}</span>{shortcutHint(name)}</button>)}<div className="border-t border-line px-2 py-1 text-ink-3">Python · 4 空格</div></>}
  </div>, document.body)
  const tools = (
    <div className="flex flex-none flex-wrap items-center gap-1 border-b border-line bg-surface px-3 py-1.5 text-[12px] text-ink-2" aria-label="编辑操作">
      {[actions[0], '格式化', ...actions.slice(1)].map((name) => <button key={name} type="button" className={`${toolbarActionClass} inline-flex items-center gap-2`} disabled={actionDisabled(name)} onClick={() => command(name)}>
        <span>{name === '格式化' && formatting ? '格式化中…' : name}</span>{shortcutHint(name)}
      </button>)}
    </div>
  )
  const assistance = (
    <>
      {editorMessage && <p role="status" className="flex-none bg-surface px-3 py-2 text-[12px] text-warn">{editorMessage}</p>}
    </>
  )
  const fileTabs = sourceTabs.length > 0 && (
    <div className="flex flex-none items-stretch overflow-x-auto border-b border-line bg-surface text-[12px]" role="tablist" aria-label="打开的文件">
      <button type="button" role="tab" aria-selected={activeSource === null} onClick={() => { setActiveSource(null); requestAnimationFrame(() => view?.focus()) }} className={`flex-none border-r border-line px-3 py-1.5 ${activeSource === null ? 'border-b-2 border-b-accent text-ink-1' : 'text-ink-2 hover:bg-fill'}`}>{workbenchTitle}</button>
      {sourceTabs.map((tab) => <div key={tab.uri} className={`flex flex-none items-center border-r border-line ${activeSource === tab.uri ? 'border-b-2 border-b-accent bg-code-bg text-ink-1' : 'text-ink-2 hover:bg-fill'}`}>
        <button type="button" role="tab" aria-selected={activeSource === tab.uri} title={decodeURIComponent(tab.uri)} onClick={() => { setActiveSource(tab.uri); requestAnimationFrame(() => sourceViews.current.get(tab.uri)?.focus()) }} className="max-w-48 truncate py-1.5 pl-3 pr-1">{sourceName(tab.uri)}</button>
        <button type="button" aria-label={`关闭 ${sourceName(tab.uri)}`} onClick={() => closeSource(tab.uri)} className="mr-1 rounded p-1 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent"><X size={13} /></button>
      </div>)}
    </div>
  )
  const sourceDocuments = sourceTabs.map((tab) => <div key={tab.uri} className={`${activeSource === tab.uri ? 'flex' : 'hidden'} min-h-0 flex-col overflow-hidden bg-code-bg ${fill ? 'flex-1' : ''}`} style={fill ? undefined : { height, minHeight, maxHeight }} aria-hidden={activeSource !== tab.uri}>
    <PythonSourcePreview source={tab} client={languageClient} kind={kind} onCursor={updateSourceCursor} onSymbols={updateSourceSymbols} onContextMenu={openContextMenu} onCreateEditor={(sourceView) => {
      sourceViews.current.set(tab.uri, sourceView)
      updateSourceCursor(tab.uri, 0, 0)
      pendingSourceViews.current.get(tab.uri)?.forEach((resolve) => resolve(sourceView))
      pendingSourceViews.current.delete(tab.uri)
    }} />
  </div>)
  const analyzerLabel = languageStatus === 'ready' ? '代码分析器' : languageStatus === 'connecting' ? '代码分析器连接中…' : '代码分析器重连中…'
  const analyzerDescription = languageStatus === 'ready' ? '代码分析器已连接' : languageStatus === 'connecting' ? '代码分析器连接中' : '暂时无法分析代码，可继续编辑'
  const statusBar = (
    <div className="flex flex-none items-center gap-3 border-t border-line bg-surface px-3 py-1 text-[11px] text-ink-3">
      {sourceTab ? <><button type="button" onClick={() => editor && gotoLine(editor)}>行 {(sourcePosition?.line ?? 0) + 1}，列 {(sourcePosition?.character ?? 0) + 1}</button><span>只读</span><span>Python</span></> : <>
      <button type="button" onClick={() => view && gotoLine(view)}>行 {position.line}，列 {position.column}</button>
      <span>4 空格</span><span>Python</span>
      <span role="status" aria-label="代码分析器" aria-description={analyzerDescription} title={analyzerDescription} data-state={languageStatus} className={`ml-auto ${languageStatus === 'reconnecting' ? 'text-warn' : ''}`}>
        <InkRewrite text={analyzerLabel} tone="label" />
      </span>
      </>}
    </div>
  )

  const codeBlock = (
    <div aria-hidden={activeSource !== null} onContextMenu={layout === 'workbench' ? (event) => { if (view) openContextMenu(event, view) } : undefined} className={`relative bg-code-bg ${activeSource === null ? '' : 'hidden'} ${fill ? 'min-h-0 flex-1' : ''}`}>
      <CodeMirror
        ref={cmRef}
        style={{ '--python-editor-font-size': `${fontSize}px` } as CSSProperties}
        className={
          fill
            ? 'h-full [&_.cm-editor]:h-full [&_.cm-editor]:!bg-code-bg [&_.cm-gutters]:!bg-code-bg [&_.cm-scroller]:h-full'
            : '[&_.cm-editor]:!bg-code-bg [&_.cm-gutters]:!bg-code-bg'
        }
        value={code}
        onChange={changeCode}
        onCreateEditor={setView}
        editable={!disabled}
        readOnly={disabled}
        height={fill ? '100%' : height}
        minHeight={fill ? undefined : minHeight}
        maxHeight={fill ? undefined : maxHeight}
        theme="none"
        extensions={extensions}
        basicSetup={basicSetup}
      />
      {!hasCode && (
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-2">
          <button className="pointer-events-auto inline-flex cursor-pointer items-center gap-2 rounded-[8px] border border-line bg-surface px-5 py-3 text-[15px] font-[550] text-ink-1 shadow-sm hover:border-ink-3" onClick={() => void paste()}>
            <Clipboard size={16} /> 从剪贴板粘贴代码
          </button>
          <span className="text-[13.5px] text-ink-3">或点此直接输入</span>
        </div>
      )}
    </div>
  )

  const currentChecks = checks?.document === code ? checks : null
  const saveBar = saveAction && (
    <PythonSaveBar action={{
      ...saveAction,
      onRestore: saveAction.onRestore ? () => {
        saveAction.onRestore?.()
        cmRef.current?.view?.focus()
      } : undefined,
    }} />
  )

  if (layout === 'workbench') {
    // 工作台：纯代码区，吃满父容器；结果呈现由外部 PythonRunPanel 承担。
    return (
      <div className="flex h-full min-h-0 w-full flex-col">
        {headerTarget ? createPortal(workbenchHeader, headerTarget) : null}
        {popup}
        {editorMessage && <p role="status" className="px-3 text-[12px] text-warn">{editorMessage}</p>}
        {fileTabs}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">{codeBlock}{sourceDocuments}</div>
        {statusTarget && createPortal(sourceTab ? <div className="flex items-center gap-2 text-[11px] text-ink-3"><button type="button" title="跳转到行" onClick={() => editor && gotoLine(editor)}>{(sourcePosition?.line ?? 0) + 1}:{(sourcePosition?.character ?? 0) + 1}</button><span>只读 · Python</span></div> : <div className="flex items-center gap-2 whitespace-nowrap text-[11px] text-ink-3"><button type="button" title="跳转到行" onClick={() => view && gotoLine(view)}><span className="max-[480px]:hidden">行 {position.line}，列 {position.column}</span><span className="hidden max-[480px]:inline">{position.line}:{position.column}</span></button><span role="status" aria-label="代码分析器" aria-description={analyzerDescription} title={analyzerDescription} data-state={languageStatus} className={languageStatus === 'reconnecting' ? 'text-warn' : ''}><InkRewrite text={analyzerLabel} tone="label" /></span></div>, statusTarget)}
        {saveBar}
      </div>
    )
  }

  // console：表单/向导内嵌布局，工具条钉在代码上方，结果带内嵌收放。
  const resultOpen = result != null
  return (
    <div className={fill ? 'flex h-full min-h-0 w-full flex-col' : 'w-full'}>
      <div
        className={`overflow-hidden rounded-[8px] border-l-[3px] ${hasCode ? style.rail : 'border-line'} ${
          fill ? 'flex min-h-0 flex-1 flex-col' : ''
        }`}
      >
        <PythonCheckStatus checks={currentChecks} onRetry={() => { if (view) forceLinting(view) }} onQuickFixRetry={() => { if (view) quickFix(view, kind) }} />
        {/* 工具条：状态句同槽换字；图标槽恒占 14px，出现/消失不推字。 */}
        <div className={`flex flex-none flex-wrap items-center gap-x-3.5 gap-y-2 border-b px-3.5 py-2.5 ${style.band}`}>
          <span className="flex min-w-[130px] flex-1 items-center gap-1.5 text-[14px] font-[520]">
            <span className="flex h-3.5 w-3.5 flex-none items-center justify-center">
              {status === 'pass' && <Check size={14} className="text-accent" />}
              {status === 'fail' && <TriangleAlert size={14} className="text-warn" />}
            </span>
            <InkRewrite text={style.body} tone="label" textClassName={style.text} />
          </span>
          {hasCode && (
            <button className="inline-flex cursor-pointer items-center gap-1.5 text-[14px] text-accent disabled:cursor-default disabled:opacity-45" onClick={() => void paste()} disabled={disabled}>
              <Clipboard size={14} /> 粘贴
            </button>
          )}
          {docHref && <a className="text-[14px] text-accent" href={docHref} target="_blank" rel="noopener">开发文档 ↗</a>}
          {controls}
          <button className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border-0 bg-ink-1 px-4 py-1.5 text-[14.5px] font-[550] text-surface disabled:cursor-default disabled:opacity-45" onClick={run} disabled={!runAllowed}>
            <Play size={14} /> {runLabel}
          </button>
        </div>

        {/* 结果区：工具条与代码之间 grid-fr 常挂收放（收放即全部连续性，不再叠 fade）；
            stale 时整体降级，旧结论不冒充新结论。权重清单长时区内自滚，不挤压代码区。 */}
        <div
          inert={!resultOpen}
          className={`flex-none grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${
            resultOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'
          }`}
        >
          <div className="min-h-0 overflow-hidden">
            {result && (
              <div className="max-h-[38vh] overflow-y-auto border-b border-line px-3.5 py-3">
                <PythonRunResultBody result={result} stale={stale} resultContent={resultContent} />
              </div>
            )}
          </div>
        </div>

        {!sourceTab && tools}{fileTabs}{assistance}{codeBlock}{sourceDocuments}{statusBar}
      </div>
      {saveBar}
    </div>
  )
}
