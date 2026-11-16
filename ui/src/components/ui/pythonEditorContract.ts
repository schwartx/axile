import type { TextEdit } from 'vscode-languageserver-protocol'
import type { LSPClient } from '@codemirror/lsp-client'

export type PythonEditorKind = 'portfolio' | 'account_notification' | 'system_notification'

export interface ContractDiagnostic {
  code?: string
  range: TextEdit['range']
  severity: number
  message: string
  source: string
}

export interface ContractFix {
  title: string
  edits: TextEdit[]
}

export interface ContractReport {
  diagnostics: ContractDiagnostic[]
  fixes: ContractFix[]
}

export async function checkPythonContract(code: string, kind: PythonEditorKind, client: Pick<LSPClient, 'request'>): Promise<ContractReport> {
  const report = await client.request<object, ContractReport>('axile/contract', { code, kind })
  if (!report || !Array.isArray(report.diagnostics) || !Array.isArray(report.fixes)) {
    throw new Error('入口契约检查响应格式无效')
  }
  return report
}
