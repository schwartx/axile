import { StateEffect, StateField, type Text } from '@codemirror/state'
import { Decoration, EditorView, ViewPlugin, WidgetType, showDialog, type DecorationSet, type ViewUpdate } from '@codemirror/view'
import { foldService } from '@codemirror/language'
import { isolateHistory } from '@codemirror/commands'
import { LSPPlugin } from '@codemirror/lsp-client'
import type { CodeAction, FoldingRange, InlayHint, SemanticTokens, SemanticTokensOptions, TextEdit, WorkspaceEdit } from 'vscode-languageserver-protocol'
import type { PythonEditorKind } from '@/components/ui/pythonEditorContract'
import { setPythonQuickFixError } from '@/components/ui/pythonChecks'
import { beginPythonQuickFixRequest, pythonRequestError, readPythonQuickFixOptions } from '@/components/ui/pythonQuickFixRequests'
import { showQuickFixMenu } from '@/components/ui/pythonQuickFixMenu'

type Visuals = { decorations: DecorationSet; folds: { from: number; to: number }[] }
const setVisuals = StateEffect.define<Visuals>()
const visuals = StateField.define<Visuals>({
  create: () => ({ decorations: Decoration.none, folds: [] }),
  update: (value, transaction) => {
    if (transaction.docChanged) value = { decorations: Decoration.none, folds: [] }
    for (const effect of transaction.effects) if (effect.is(setVisuals)) value = effect.value
    return value
  },
  provide: (field) => EditorView.decorations.from(field, (value) => value.decorations),
})

class Hint extends WidgetType {
  constructor(readonly text: string, readonly tooltip: string) { super() }
  eq(other: Hint) { return this.text === other.text && this.tooltip === other.tooltip }
  toDOM() {
    const span = document.createElement('span')
    span.className = 'cm-python-inlay'
    span.textContent = this.text
    span.title = this.tooltip
    return span
  }
}

const semanticColors: Record<string, string> = {
  namespace: 'type', class: 'type', enum: 'type', interface: 'type', struct: 'type', type: 'type', typeParameter: 'type',
  function: 'function', method: 'function', variable: 'name', parameter: 'name', property: 'name',
  keyword: 'keyword', string: 'string', number: 'number', comment: 'comment', decorator: 'function',
}

export function semanticDecorations(doc: Text, data: number[], types: string[]) {
  const marks = []
  let line = 0, column = 0
  for (let index = 0; index + 4 < data.length; index += 5) {
    const delta = data[index]
    line += delta
    column = delta ? data[index + 1] : column + data[index + 1]
    const length = data[index + 2], color = semanticColors[types[data[index + 3]]]
    if (!color || line >= doc.lines) continue
    const row = doc.line(line + 1)
    if (length <= 0 || column + length > row.length) continue
    marks.push(Decoration.mark({ class: `cm-python-semantic-${color}` }).range(row.from + column, row.from + column + length))
  }
  return marks
}

function makeVisuals(plugin: LSPPlugin, doc: Text, tokens: SemanticTokens | null, hints: InlayHint[], folds: FoldingRange[]): Visuals {
  const provider = plugin.client.serverCapabilities?.semanticTokensProvider as SemanticTokensOptions | undefined
  const marks = semanticDecorations(doc, tokens?.data ?? [], provider?.legend.tokenTypes ?? [])
  for (const hint of hints) {
    if (hint.position.line >= doc.lines) continue
    const label = typeof hint.label === 'string' ? hint.label : hint.label.map((part) => part.value).join('')
    const text = `${hint.paddingLeft ? ' ' : ''}${label}${hint.paddingRight ? ' ' : ''}`
    const tooltip = typeof hint.tooltip === 'string' ? hint.tooltip : hint.tooltip?.value ?? ''
    marks.push(Decoration.widget({ widget: new Hint(text, tooltip), side: 1 }).range(plugin.fromPosition(hint.position, doc)))
  }
  return {
    decorations: Decoration.set(marks, true),
    folds: folds.filter((range) => range.startLine < range.endLine && range.endLine < doc.lines).map((range) => ({
      from: doc.line(range.startLine + 1).from + (range.startCharacter ?? doc.line(range.startLine + 1).length),
      to: doc.line(range.endLine + 1).from + (range.endCharacter ?? doc.line(range.endLine + 1).length),
    })),
  }
}

/** 语义着色、类型提示与服务端折叠共享一轮防抖，旧文档的响应不落到新文档。 */
export function pythonVisualFeatures() {
  return [
    visuals,
    foldService.of((state, start, end) => state.field(visuals).folds.find((range) => range.from >= start && range.from <= end) ?? null),
    EditorView.theme({
      '.cm-python-inlay': { color: 'var(--color-ink-3)', fontSize: '0.9em', padding: '0 2px' },
      ...Object.fromEntries([...new Set(Object.values(semanticColors))].map((color) => [`.cm-python-semantic-${color}`, { color: `var(--color-code-${color})` }])),
    }),
    ViewPlugin.fromClass(class {
      timer: ReturnType<typeof setTimeout> | undefined
      alive = true
      constructor(readonly view: EditorView) { this.schedule() }
      update(update: ViewUpdate) { if (update.docChanged) this.schedule() }
      schedule() { clearTimeout(this.timer); this.timer = setTimeout(() => void this.refresh(), 450) }
      async refresh() {
        const plugin = LSPPlugin.get(this.view)
        if (!plugin?.client.connected) return
        const doc = this.view.state.doc, textDocument = { uri: plugin.uri }
        plugin.client.sync()
        const caps = plugin.client.serverCapabilities
        const results = await Promise.allSettled([
          caps?.semanticTokensProvider ? plugin.client.request<object, SemanticTokens>('textDocument/semanticTokens/full', { textDocument }) : Promise.resolve(null),
          caps?.inlayHintProvider ? plugin.client.request<object, InlayHint[]>('textDocument/inlayHint', { textDocument, range: { start: { line: 0, character: 0 }, end: plugin.toPosition(doc.length, doc) } }) : Promise.resolve([]),
          caps?.foldingRangeProvider ? plugin.client.request<object, FoldingRange[]>('textDocument/foldingRange', { textDocument }) : Promise.resolve([]),
        ])
        if (!this.alive || this.view.state.doc !== doc) return
        const [tokens, hints, folds] = results
        this.view.dispatch({ effects: setVisuals.of(makeVisuals(plugin, doc,
          tokens.status === 'fulfilled' ? tokens.value : null,
          hints.status === 'fulfilled' ? hints.value ?? [] : [],
          folds.status === 'fulfilled' ? folds.value ?? [] : [],
        )) })
      }
      destroy() { this.alive = false; clearTimeout(this.timer) }
    }),
  ]
}

/** 第一版只修改用户草稿；依赖源码保持只读，跨文件修改整体拒绝。 */
export function workspaceEdits(edit: WorkspaceEdit, uri: string): TextEdit[] {
  const edits: TextEdit[] = []
  for (const [file, changes] of Object.entries(edit.changes ?? {})) {
    if (file !== uri) throw new Error('此操作需要修改依赖源码，当前仅支持修改草稿')
    edits.push(...changes)
  }
  for (const change of edit.documentChanges ?? []) {
    if (!('textDocument' in change) || change.textDocument.uri !== uri) throw new Error('不支持跨文件修改')
    for (const item of change.edits) {
      if (!('newText' in item)) throw new Error('不支持片段形式的工作区修改')
      edits.push(item)
    }
  }
  return edits
}

export function applyWorkspaceEdit(view: EditorView, edit: WorkspaceEdit, original: Text) {
  const plugin = LSPPlugin.get(view)
  if (!plugin || view.state.doc !== original) throw new Error('代码已变化，请重新执行操作')
  if (view.state.readOnly) throw new Error('当前文档只读')
  const changes = workspaceEdits(edit, plugin.uri).map((item) => ({
    from: plugin.fromPosition(item.range.start, original), to: plugin.fromPosition(item.range.end, original), insert: item.newText,
  }))
  view.dispatch({ changes, annotations: isolateHistory.of('full'), userEvent: 'input' })
  view.focus()
}

export function renamePythonSymbol(view: EditorView): boolean {
  const plugin = LSPPlugin.get(view)
  const word = view.state.wordAt(view.state.selection.main.head)
  if (!plugin || !word || view.state.readOnly) return false
  const doc = view.state.doc, position = plugin.toPosition(word.from)
  const dialog = showDialog(view, { label: '新名称', input: { name: 'name', value: doc.sliceString(word.from, word.to) }, submitLabel: '重命名', focus: true })
  void dialog.result.then(async (form) => {
    view.dispatch({ effects: dialog.close })
    if (!form) return
    try {
      if (view.state.doc !== doc) throw new Error('代码已变化，请重新重命名')
      plugin.client.sync()
      const edit = await plugin.client.request<object, WorkspaceEdit | null>('textDocument/rename', {
        textDocument: { uri: plugin.uri }, position, newName: (form.elements.namedItem('name') as HTMLInputElement).value,
      })
      if (edit) applyWorkspaceEdit(view, edit, doc)
    } catch (error) { plugin.reportError('重命名失败', error) }
  })
  return true
}

export function quickFix(view: EditorView, kind: PythonEditorKind, from?: number, to?: number, anchor?: DOMRect, host?: HTMLElement, onDismiss?: () => void): boolean {
  const plugin = LSPPlugin.get(view)
  if (!plugin || view.state.readOnly) { onDismiss?.(); return false }
  const doc = view.state.doc
  const latestRequest = beginPythonQuickFixRequest(view)
  const current = () => latestRequest() && view.state.doc === doc && LSPPlugin.get(view) === plugin && plugin.client.connected
  const reportError = (cause: unknown) => {
    if (current()) view.dispatch({ effects: setPythonQuickFixError.of(pythonRequestError(cause)) })
  }
  const targetFrom = from ?? view.state.selection.main.from
  const targetTo = to ?? view.state.selection.main.to
  const range = { start: plugin.toPosition(targetFrom), end: plugin.toPosition(targetTo) }
  void (async () => {
    try {
      plugin.client.sync()
      const options = await readPythonQuickFixOptions(plugin.client, { uri: plugin.uri, code: doc.toString(), kind, range })
      if (!current()) return
      view.dispatch({ effects: setPythonQuickFixError.of(options.error) })
      const fixes = options.actions.filter((item): item is CodeAction => typeof item.command !== 'string' && !('disabled' in item && item.disabled))
      const cursor = plugin.toPosition(targetFrom)
      for (const fix of options.contract?.fixes ?? []) {
        const annotation = fix.edits[0]
        if (!annotation || annotation.range.start.line !== cursor.line) continue
        fixes.push({ title: fix.title, kind: 'quickfix', edit: { changes: { [plugin.uri]: fix.edits } } })
      }
      const point = anchor ?? view.coordsAtPos(targetFrom) ?? view.dom.getBoundingClientRect()
      showQuickFixMenu(view, point, fixes.map((fix) => ({
        title: fix.title,
        apply: () => { void (async () => {
          try {
            const resolved = fix.edit ? fix : await plugin.client.request<CodeAction, CodeAction>('codeAction/resolve', fix)
            if (!current()) return
            if (!resolved.edit) throw new Error('此修复没有可应用的代码修改')
            applyWorkspaceEdit(view, resolved.edit, doc)
          } catch (error) { reportError(error) }
        })() },
      })), host?.isConnected ? host : undefined, onDismiss, options.error !== null)
    } catch (error) { if (current()) { onDismiss?.(); reportError(error) } }
  })()
  return true
}
