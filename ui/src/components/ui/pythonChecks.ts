import { StateEffect } from '@codemirror/state'
import type { Diagnostic } from 'vscode-languageserver-protocol'
import type { ContractDiagnostic } from '@/components/ui/pythonEditorContract'
import { pythonRequestError } from '@/components/ui/pythonQuickFixRequests'

export interface PythonCheckSource {
  status: 'checking' | 'succeeded' | 'failed'
  diagnostics: (Diagnostic | ContractDiagnostic)[]
  error: Error | null
}
export interface PythonChecks {
  document: string
  version: number
  generation: number
  lsp: PythonCheckSource
  contract: PythonCheckSource
  quickFixError: Error | null
}
/** 快捷键、悬停按钮与后台修复能力查询共用持久错误槽。 */
export const setPythonQuickFixError = StateEffect.define<Error | null>()
export const checkingSource = (): PythonCheckSource => ({ status: 'checking', diagnostics: [], error: null })
export function checkSource(result: PromiseSettledResult<(Diagnostic | ContractDiagnostic)[]>): PythonCheckSource {
  return result.status === 'fulfilled' ? { status: 'succeeded', diagnostics: result.value, error: null }
    : { status: 'failed', diagnostics: [], error: pythonRequestError(result.reason) }
}
export function checksAreEmpty(checks: PythonChecks | null): boolean {
  return !!checks && checks.lsp.status === 'succeeded' && checks.contract.status === 'succeeded' && !checks.lsp.diagnostics.length && !checks.contract.diagnostics.length
}
