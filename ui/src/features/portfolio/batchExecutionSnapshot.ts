import { errorInfo, errorInfoFromEntries, errorValueText, type ErrorEvidence } from '@/lib/errorInfo'
import type { ExecutionTrigger } from '@/types/api'

export interface BatchResult {
  accountId: number
  name: string
  trigger: ExecutionTrigger | null
  error: Error | null
  pending: boolean
}

export function batchResultStatus(result: BatchResult): string {
  if (result.pending) return '正在提交'
  if (!result.trigger) return '受理失败'
  return result.trigger.accepted === 'coalesced' ? '已合并受理' : '已受理'
}

/** 打开时转换为字符串快照，逐账户保留受理响应与完整错误。 */
export function batchExecutionInfo(portfolioId: number, results: BatchResult[]) {
  const pending = results.filter(result => result.pending).length
  const details: ErrorEvidence[] = results.flatMap(result => {
    const label = `${result.name} (#${result.accountId}) · ${batchResultStatus(result)}`
    if (result.error) return [{ label, value: errorInfo(result.error, [{ label: '账户 ID', value: String(result.accountId) }]).text }]
    if (result.trigger) return [{ label, value: errorValueText(result.trigger) }]
    return [{ label, value: '请求尚未返回，受理结果待确认。' }]
  })
  return errorInfoFromEntries('批量执行受理结果', [
    { label: '组合 ID', value: String(portfolioId) },
    { label: '范围', value: `打开详情时的 ${results.length} 个账户结果；${pending ? `${pending} 个仍在提交，结果尚未齐全` : '全部请求已返回'}。受理成功后需继续查看执行状态。` },
  ], details)
}
