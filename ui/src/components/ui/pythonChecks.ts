import type { Diagnostic } from 'vscode-languageserver-protocol'
import type { ContractDiagnostic } from '@/components/ui/pythonEditorContract'

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
}
export const checkingSource = (): PythonCheckSource => ({ status: 'checking', diagnostics: [], error: null })
export function checkSource(result: PromiseSettledResult<(Diagnostic | ContractDiagnostic)[]>): PythonCheckSource {
  return result.status === 'fulfilled' ? { status: 'succeeded', diagnostics: result.value, error: null }
    : { status: 'failed', diagnostics: [], error: result.reason instanceof Error ? result.reason : new Error(String(result.reason)) }
}
export function checksAreEmpty(checks: PythonChecks | null): boolean {
  return !!checks && checks.lsp.status === 'succeeded' && checks.contract.status === 'succeeded' && !checks.lsp.diagnostics.length && !checks.contract.diagnostics.length
}
