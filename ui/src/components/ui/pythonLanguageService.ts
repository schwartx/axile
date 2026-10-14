import { checkingSource, checkSource, setPythonQuickFixError, type PythonChecks, type PythonCheckSource } from '@/components/ui/pythonChecks'
import { beginPythonQuickFixRequest, readPythonCodeActions } from '@/components/ui/pythonQuickFixRequests'
import { Compartment, Text } from '@codemirror/state'
import { EditorView, ViewPlugin, keymap, showDialog, type ViewUpdate } from '@codemirror/view'
import { linter, forceLinting, type Diagnostic } from '@codemirror/lint'
import { LSPClient, LSPPlugin, serverCompletion, signatureHelp, jumpToDefinitionKeymap, findReferencesKeymap, type WorkspaceFile } from '@codemirror/lsp-client'
import DOMPurify from 'dompurify'
import { pythonHover } from '@/components/ui/pythonHover'
import { apiGet } from '@/lib/api/client'
import { checkPythonContract, type PythonEditorKind } from '@/components/ui/pythonEditorContract'
import { pythonVisualFeatures, quickFix, renamePythonSymbol } from '@/components/ui/pythonLanguageFeatures'
import type { Diagnostic as ProtocolDiagnostic, DocumentSymbol, SymbolInformation } from 'vscode-languageserver-protocol'
import { actionTargetsDiagnostic } from '@/components/ui/pythonQuickFixAvailability'
import { pythonDefinitionClick } from '@/components/ui/pythonDefinitionClick'

export type LanguageStatus = 'connecting' | 'ready' | 'reconnecting'
export type DocumentSymbols = DocumentSymbol[] | SymbolInformation[]
/** 重连时只替换语言扩展，保留编辑文档、选择和撤销栈。 */
export function connectPython(
  view: EditorView,
  slot: Compartment,
  kind: PythonEditorKind,
  onStatus: (status: LanguageStatus) => void,
  displaySource: (uri: string, code: string) => Promise<EditorView | null>,
  onSymbols?: (symbols: DocumentSymbols | null, doc: Text) => void,
  onClient?: (client: LSPClient | null) => void,
  onChecks?: (checks: PythonChecks) => void,
) {
  let disposed = false
  let socket: WebSocket | null = null
  let client: LSPClient | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let watchdog: ReturnType<typeof setTimeout> | undefined
  let failures = 0

  let analysisGeneration = 0
  let documentVersion = 0
  let connectionError = new Error('代码分析器尚未连接')
  let latestChecks: PythonChecks | null = null
  const publishChecks = (checks: PythonChecks) => { latestChecks = checks; onChecks?.(checks) }
  const offset = (doc: Text, position: { line: number; character: number }) => {
    const line = doc.line(Math.min(Math.max(position.line + 1, 1), doc.lines))
    return Math.min(line.to, line.from + Math.max(position.character, 0))
  }
  const analysis = (active: LSPClient | null, uri: string) => linter(async (editor): Promise<Diagnostic[]> => {
    const plugin = LSPPlugin.get(editor)
    const doc = editor.state.doc
    const generation = ++analysisGeneration
    const quickFixCurrent = beginPythonQuickFixRequest(editor)
    const version = documentVersion
    const initial: PythonChecks = { document: doc.toString(), version, generation, lsp: checkingSource(), contract: checkingSource(), quickFixError: null }
    publishChecks(initial)
    const current = () => !disposed && editor.state.doc === doc && generation === analysisGeneration
    const updateChecks = (patch: Partial<PythonChecks>) => {
      if (current() && latestChecks?.generation === generation) publishChecks({ ...latestChecks, ...patch })
    }
    try {
      const observe = <T>(source: 'lsp' | 'contract', request: Promise<T>, diagnostics: (value: T) => PythonCheckSource['diagnostics']) => request.then(value => {
        updateChecks({ [source]: checkSource({ status: 'fulfilled', value: diagnostics(value) }) })
        return value
      }, reason => {
        updateChecks({ [source]: checkSource({ status: 'rejected', reason }) })
        throw reason
      })
      const tyRead = Promise.resolve().then(() => {
        if (!active?.connected) throw connectionError
        active.sync()
        return active.request<object, { items?: ProtocolDiagnostic[] }>('textDocument/diagnostic', { textDocument: { uri } })
      })
      const [tyResult, contractResult] = await Promise.allSettled([
        observe('lsp', tyRead, value => value.items ?? []),
        observe('contract', checkPythonContract(doc.toString(), kind), value => value.diagnostics),
      ])
      if (disposed || editor.state.doc !== doc || generation !== analysisGeneration) return []
      const tyItems = tyResult.status === 'fulfilled' ? tyResult.value.items ?? [] : []
      const contractItems = contractResult.status === 'fulfilled' ? contractResult.value.diagnostics : []
      const actionResult = tyItems.length && active && plugin && quickFixCurrent() ? await readPythonCodeActions(active, {
        textDocument: { uri },
        range: { start: { line: 0, character: 0 }, end: plugin.toPosition(doc.length) },
        context: { diagnostics: tyItems, only: ['quickfix'], triggerKind: 1 },
      }) : { actions: [], error: null }
      if (disposed || editor.state.doc !== doc || generation !== analysisGeneration) return []
      if (actionResult.error && quickFixCurrent()) updateChecks({ quickFixError: actionResult.error })
      return [...tyItems, ...contractItems].map((item) => {
        const isContract = 'source' in item && item.source === '入口契约'
        const canFix = !editor.state.readOnly && (isContract
          ? item.severity === 3 && (contractResult.status === 'fulfilled' && contractResult.value.fixes.some((fix) => fix.edits[0]?.range.start.line === item.range.start.line))
          : actionResult.actions.some((action) => actionTargetsDiagnostic(action, item)))
        return {
          from: offset(doc, item.range.start),
          to: offset(doc, item.range.end),
          severity: item.severity === 1 ? 'error' : item.severity === 2 ? 'warning' : 'info',
          message: typeof item.message === 'string' ? item.message : item.message.value,
          source: isContract ? '入口契约' : 'ty',
          actions: canFix ? [{ name: '快速修复', apply: (target: EditorView, from: number, to: number) => { quickFix(target, kind, from, to) } }] : [],
        } satisfies Diagnostic
      })
    } catch (cause) {
      if (!disposed && editor.state.doc === doc && generation === analysisGeneration) {
        const failed = checkSource({ status: 'rejected', reason: cause })
        updateChecks({ lsp: failed, contract: failed })
      }
      return []
    }
  }, {
    delay: 350,
    // 诊断仍留在问题列表和代码标记中；悬停内容统一由 pythonHover 渲染。
    tooltipFilter: () => [],
  })
  const watcher = ViewPlugin.fromClass(class {
    update(update: ViewUpdate) {
      for (const transaction of update.transactions) for (const effect of transaction.effects) {
        if (effect.is(setPythonQuickFixError) && latestChecks?.document === update.state.doc.toString()) {
          publishChecks({ ...latestChecks, quickFixError: effect.value })
        }
      }
      if (!update.docChanged) return
      documentVersion += 1
      analysisGeneration += 1
      queueMicrotask(() => {
        if (!disposed && view.state.doc === update.state.doc) publishChecks({ document: update.state.doc.toString(), version: documentVersion, generation: analysisGeneration, lsp: checkingSource(), contract: checkingSource(), quickFixError: null })
      })
    }
  })
  view.dispatch({ effects: slot.reconfigure([watcher, analysis(null, '')]) })

  const connect = () => {
    if (disposed) return
    onStatus(failures ? 'reconnecting' : 'connecting')
    const url = new URL('/api/v1/editor/lsp', window.location.href)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(url)
    socket = ws
    const handlers = new Set<(message: string) => void>()
    watchdog = setTimeout(() => ws.close(), 15000)
    ws.onmessage = (event) => {
      if (disposed) return
      const message = JSON.parse(String(event.data))
      if (message.type !== 'session') {
        handlers.forEach((handler) => handler(String(event.data)))
        return
      }
      const active = new LSPClient({
        rootUri: message.rootUri,
        timeout: 10000,
        sanitizeHTML: (html) => DOMPurify.sanitize(html),
        extensions: [serverCompletion(), signatureHelp(), {
          clientCapabilities: { textDocument: {
            codeAction: { codeActionLiteralSupport: { codeActionKind: { valueSet: ['quickfix'] } }, resolveSupport: { properties: ['edit'] } },
            semanticTokens: { requests: { full: true }, tokenTypes: ['namespace', 'class', 'type', 'function', 'method', 'variable', 'parameter', 'property', 'keyword', 'string', 'number', 'comment', 'decorator'], tokenModifiers: [], formats: ['relative'] },
            inlayHint: {}, foldingRange: { lineFoldingOnly: false }, documentSymbol: { hierarchicalDocumentSymbolSupport: true },
          } },
        }],
      })
      client = active
      const workspace = active.workspace
      const sources = new Map<string, WorkspaceFile>()
      workspace.requestFile = async (uri) => {
        const existing = workspace.getFile(uri) ?? sources.get(uri)
        if (existing) return existing
        const { code } = await apiGet<{ code: string }>(`/editor/source?uri=${encodeURIComponent(uri)}`)
        const file: WorkspaceFile = { uri, languageId: 'python', version: 0, doc: Text.of(code.split('\n')), getView: () => null }
        sources.set(uri, file)
        return file
      }
      workspace.displayFile = async (uri) => {
        try {
          const own = workspace.getFile(uri)?.getView()
          if (own) return displaySource(uri, own.state.doc.toString())
          const file = await workspace.requestFile(uri)
          return file && !disposed ? displaySource(uri, file.doc.toString()) : null
        } catch (error) {
          if (!disposed) showDialog(view, { label: error instanceof Error ? error.message : '无法打开源码预览' })
          return null
        }
      }
      active.connect({
        send: (value) => {
          if (ws.readyState !== WebSocket.OPEN) throw new Error('语言服务已断开')
          ws.send(value)
        },
        subscribe: (handler) => { handlers.add(handler) },
        unsubscribe: (handler) => { handlers.delete(handler) },
      })
      void active.initializing.then(() => {
        if (disposed || socket !== ws || ws.readyState !== WebSocket.OPEN) return
        clearTimeout(watchdog)
        failures = 0
        view.dispatch({ effects: slot.reconfigure([
          watcher,
          active.plugin(message.uri, 'python'),
          pythonVisualFeatures(),
          pythonHover(kind),
          pythonDefinitionClick,
          ...(onSymbols ? [documentSymbols(onSymbols)] : []),
          keymap.of([...jumpToDefinitionKeymap, ...findReferencesKeymap,
            { key: 'F2', run: renamePythonSymbol }, { key: 'Mod-.', run: (editor) => quickFix(editor, kind) }]),
          analysis(active, message.uri),
        ]) })
        forceLinting(view)
        onClient?.(active)
        onStatus('ready')
      }).catch(() => ws.close())
    }
    ws.onerror = () => ws.close()
    ws.onclose = (event) => {
      connectionError = new Error(`代码分析器连接关闭：${event.code} ${event.reason}`)
      clearTimeout(watchdog)
      analysisGeneration += 1
      client?.disconnect()
      client = null
      if (disposed) return
      onClient?.(null)
      view.dispatch({ effects: slot.reconfigure([watcher, analysis(null, '')]) })
      forceLinting(view)
      onStatus('reconnecting')
      timer = setTimeout(connect, Math.min(1000 * 2 ** failures++, 15000))
    }
  }
  connect()
  return () => {
    disposed = true
    analysisGeneration += 1
    onClient?.(null)
    clearTimeout(timer)
    clearTimeout(watchdog)
    client?.disconnect()
    socket?.close()
  }
}

/** 文档版本与插件身份共同约束响应，编辑后立即清除旧的可点击位置。 */
export function documentSymbols(onSymbols: (symbols: DocumentSymbols | null, doc: Text) => void) {
  return ViewPlugin.fromClass(class {
    timer: ReturnType<typeof setTimeout> | undefined
    sequence = 0
    alive = true
    constructor(readonly view: EditorView) { this.schedule(0) }
    update(update: ViewUpdate) {
      if (update.docChanged) {
        this.sequence++
        onSymbols(null, update.state.doc)
        this.schedule(450)
      }
    }
    schedule(delay: number) {
      clearTimeout(this.timer)
      this.timer = setTimeout(() => void this.refresh(), delay)
    }
    async refresh() {
      const plugin = LSPPlugin.get(this.view)
      const doc = this.view.state.doc
      const sequence = ++this.sequence
      if (!plugin?.client.connected || !plugin.client.serverCapabilities?.documentSymbolProvider) {
        onSymbols(null, doc)
        return
      }
      try {
        plugin.client.sync()
        const symbols = await plugin.client.request<object, DocumentSymbols | null>('textDocument/documentSymbol', { textDocument: { uri: plugin.uri } })
        if (this.alive && this.sequence === sequence && this.view.state.doc === doc && LSPPlugin.get(this.view) === plugin && plugin.client.connected) onSymbols(symbols ?? [], doc)
      } catch {
        if (this.alive && this.sequence === sequence && this.view.state.doc === doc) onSymbols(null, doc)
      }
    }
    destroy() { this.alive = false; this.sequence++; clearTimeout(this.timer); onSymbols(null, this.view.state.doc) }
  })
}
