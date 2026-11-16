import init, { Workspace, PositionEncoding, SemanticToken, type FileHandle } from './wasm/ty_wasm'
import { Decompress } from 'fzstd'

const MAX_BYTES = 2 * 1024 * 1024
const draftUri = 'file:///draft/portfolio.py'
const files = new Map<string, { handle: FileHandle; code: string; readonly: boolean }>()
let workspace: Workspace

function zstdStream() {
  let decoder: Decompress
  return new TransformStream<Uint8Array, Uint8Array>({
    start(controller) { decoder = new Decompress(chunk => controller.enqueue(chunk)) },
    transform(chunk) { decoder.push(chunk) },
    flush() { decoder.push(new Uint8Array(), true) },
  })
}

async function initialize(wasm: ArrayBuffer, libraryBytes: ArrayBuffer) {
  if (!(wasm instanceof ArrayBuffer) || !(libraryBytes instanceof ArrayBuffer)) throw Error('请先下载完整的代码辅助组件')
  const wasmStream = zstdStream()
  await init({ module_or_path: new Response(new Response(wasm).body!.pipeThrough(wasmStream), { headers: { 'Content-Type': 'application/wasm' } }) })
  workspace = new Workspace('/draft', PositionEncoding.Utf16, {})
  const library: { files: Record<string, string> } = await new Response(new Response(libraryBytes).body!.pipeThrough(zstdStream())).json()
  for (const [path, code] of Object.entries(library.files)) {
    files.set(`file://${path}`, { handle: workspace.openFile(path, code), code, readonly: true })
  }
  workspace.updateOptions({ environment: { 'extra-paths': ['/lib'], 'python-version': '3.12' } })
}
let ready: Promise<void>
const failed = (cause: unknown) => postMessage(JSON.stringify({ type: 'failed', message: cause instanceof Error ? cause.message : String(cause) }))

function getFile(uri: string) {
  const existing = files.get(uri)
  if (existing) return existing
  if (uri.startsWith('vendored://')) {
    const path = decodeURIComponent(uri.slice('vendored://'.length))
    const handle = workspace.getVendoredFile(path)
    const file = { handle, code: workspace.sourceText(handle), readonly: true }
    files.set(uri, file)
    return file
  }
  throw new Error('文档不属于当前本地编辑工作台')
}

function open(uri: string, code: string) {
  if (new TextEncoder().encode(code).length > MAX_BYTES) throw new Error('文档过大')
  const existing = files.get(uri)
  if (existing?.readonly) {
    if (existing.code !== code) throw new Error('依赖源码只读')
    return
  }
  if (uri !== draftUri) throw new Error('只能修改当前草稿')
  if (existing) {
    workspace.updateFile(existing.handle, code)
    existing.code = code
  } else {
    files.set(uri, { handle: workspace.openFile('/draft/portfolio.py', code), code, readonly: false })
  }
}

type Position = { line: number; character: number }
function offset(code: string, position: Position) {
  const lines = code.split('\n')
  if (!Number.isInteger(position.line) || position.line < 0 || position.line >= lines.length
    || !Number.isInteger(position.character) || position.character < 0 || position.character > lines[position.line]!.length) throw new Error('无效文档位置')
  return lines.slice(0, position.line).reduce((sum, line) => sum + line.length + 1, 0) + position.character
}

async function dispatch(raw: string) {
  const message = JSON.parse(raw)
  try {
    await ready
    const { method, params = {} } = message
    let result: unknown = null
    if (method === 'initialize') {
      result = { capabilities: {
        positionEncoding: 'utf-16', textDocumentSync: { openClose: true, change: 1 },
        completionProvider: { triggerCharacters: ['.'] }, hoverProvider: true, signatureHelpProvider: { triggerCharacters: ['(', ','] },
        definitionProvider: true, referencesProvider: true, renameProvider: { prepareProvider: true },
        diagnosticProvider: { interFileDependencies: true, workspaceDiagnostics: false },
        codeActionProvider: { codeActionKinds: ['quickfix'] }, inlayHintProvider: true,
        documentSymbolProvider: true, foldingRangeProvider: true,
        semanticTokensProvider: { legend: { tokenTypes: SemanticToken.kinds(), tokenModifiers: SemanticToken.modifiers() }, full: true },
      }, serverInfo: { name: 'Axile local ty' } }
    } else if (['initialized', '$/cancelRequest', 'exit', 'shutdown'].includes(method)) {
      // Synchronous WASM work finishes before the next message; stale UI results are version-gated.
    } else if (method === 'textDocument/didOpen') {
      open(params.textDocument.uri, params.textDocument.text)
    } else if (method === 'textDocument/didChange') {
      const uri = params.textDocument.uri
      let code = getFile(uri).code
      for (const change of params.contentChanges) {
        code = change.range ? code.slice(0, offset(code, change.range.start)) + change.text + code.slice(offset(code, change.range.end)) : change.text
      }
      open(uri, code)
    } else if (method === 'textDocument/didClose') {
      const uri = params.textDocument.uri
      const file = files.get(uri)
      if (file && !file.readonly) { workspace.closeFile(file.handle); files.delete(uri) }
    } else if (['completionItem/resolve', 'codeAction/resolve', 'inlayHint/resolve'].includes(method)) {
      result = params
    } else if (method === 'axile/contract' || method === 'axile/format') {
      if (typeof params.code !== 'string' || new TextEncoder().encode(params.code).length > MAX_BYTES) throw new Error('无效或过大的文档')
      const handle = workspace.openFile('/scratch.py', params.code)
      try { result = JSON.parse(workspace.axileRequest(method, handle, JSON.stringify(params))) }
      finally { workspace.closeFile(handle) }
    } else {
      result = JSON.parse(workspace.axileRequest(method, getFile(params.textDocument.uri).handle, JSON.stringify(params)))
    }
    if (message.id !== undefined) postMessage(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }))
  } catch (cause) {
    const error = { code: -32603, message: cause instanceof Error ? cause.message : String(cause) }
    if (message.id !== undefined) postMessage(JSON.stringify({ jsonrpc: '2.0', id: message.id, error }))
    else throw cause
  }
}

// Preserve didChange/request order even while WASM and the type library are loading.
let pending = Promise.resolve()
self.onmessage = (event: MessageEvent<string | { kind: 'init'; wasm: ArrayBuffer; library: ArrayBuffer }>) => {
  if (typeof event.data !== 'string') {
    if (event.data.kind === 'init' && !ready) {
      ready = initialize(event.data.wasm, event.data.library)
      void ready.then(() => postMessage(JSON.stringify({ type: 'ready' })), failed)
    }
    return
  }
  const message = event.data
  pending = pending.then(() => dispatch(message)).catch(failed)
}
