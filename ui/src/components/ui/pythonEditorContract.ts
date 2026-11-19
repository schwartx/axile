import type { TextEdit } from 'vscode-languageserver-protocol'
import { ApiError, apiSend } from '@/lib/api/client'

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

export async function checkPythonContract(code: string, kind: PythonEditorKind): Promise<ContractReport> {
  const report = await apiSend<ContractReport>('POST', '/editor/contract', { code, kind })
  if (!report || !Array.isArray(report.diagnostics) || !Array.isArray(report.fixes)) {
    throw new ApiError(200, '入口契约检查响应格式无效', { body: report })
  }
  return report
}
