import type { LSPClient } from '@codemirror/lsp-client'
import type { EditorView } from '@codemirror/view'
import type { CodeAction, CodeActionParams, Command, Diagnostic, Range } from 'vscode-languageserver-protocol'
import { checkPythonContract, type PythonEditorKind } from '@/components/ui/pythonEditorContract'
import { errorInfo, errorValueText } from '@/lib/errorInfo'

/** LSP 按 JSON-RPC 协议拒绝普通对象，message 与全部响应字段都需要保留。 */
export function pythonRequestError(cause: unknown): Error {
  if (cause instanceof Error) return cause
  const raw = errorValueText(cause)
  const message = cause && typeof cause === 'object' && 'message' in cause && typeof cause.message === 'string'
    ? `${cause.message}\n\n错误响应\n${raw}` : raw
  return new Error(message)
}
const requests = new WeakMap<EditorView, number>()

/** 后台检查与显式重试共享代次，迟到的旧失败不能覆盖新结果。 */
export function beginPythonQuickFixRequest(view: EditorView): () => boolean {
  const generation = (requests.get(view) ?? 0) + 1
  requests.set(view, generation)
  return () => requests.get(view) === generation
}

/** 修复能力读取失败保留原始错误，与有效诊断分别处理。 */
export async function readPythonCodeActions(client: Pick<LSPClient, 'request'>, params: CodeActionParams) {
  try {
    const actions = await client.request<CodeActionParams, (CodeAction | Command)[] | null>('textDocument/codeAction', params)
    if (actions != null && (!Array.isArray(actions) || actions.some(action => !action || typeof action !== 'object' || typeof action.title !== 'string'))) {
      throw new Error(`快速修复响应格式无效\n${errorValueText(actions)}`)
    }
    return { actions: actions ?? [], error: null }
  } catch (cause) {
    return { actions: [] as (CodeAction | Command)[], error: pythonRequestError(cause) }
  }
}

/** 某一来源失败时仍保留其他来源的修复；多处失败的原文全部可读。 */
export async function readPythonQuickFixOptions(client: Pick<LSPClient, 'request'>, input: {
  uri: string; code: string; kind: PythonEditorKind; range: Range
}, contractCheck = checkPythonContract) {
  const textDocument = { uri: input.uri }
  const [contract, diagnostics] = await Promise.allSettled([
    contractCheck(input.code, input.kind),
    client.request<object, { items?: Diagnostic[] }>('textDocument/diagnostic', { textDocument }),
  ])
  const actions = await readPythonCodeActions(client, {
    textDocument, range: input.range,
    context: { diagnostics: diagnostics.status === 'fulfilled' ? diagnostics.value.items ?? [] : [], only: ['quickfix'], triggerKind: 1 },
  })
  const failures = [
    ...(contract.status === 'rejected' ? [{ source: '入口契约', error: pythonRequestError(contract.reason) }] : []),
    ...(diagnostics.status === 'rejected' ? [{ source: 'LSP 诊断', error: pythonRequestError(diagnostics.reason) }] : []),
    ...(actions.error ? [{ source: 'LSP 快速修复', error: actions.error }] : []),
  ]
  const error = failures.length === 1 ? failures[0]!.error : failures.length > 1
    ? new Error(failures.map(failure => `${failure.source}\n${errorInfo(failure.error).text}`).join('\n\n')) : null
  return { actions: actions.actions, contract: contract.status === 'fulfilled' ? contract.value : null, error }
}
