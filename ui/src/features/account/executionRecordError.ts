import { shortErrorReason } from '@/lib/errorInfo'

/** 执行记录顶层错误的人读短原因；无错误时返回空串。 */
export function executionRecordError(record: { display_error?: string | null; error?: string | null; interrupt_reason?: unknown; result?: unknown }): string {
  const result = record.result != null && typeof record.result === 'object' && !Array.isArray(record.result) ? record.result as Record<string, unknown> : {}
  const interruptReason = Object.hasOwn(record, 'interrupt_reason') ? record.interrupt_reason : result.interrupt_reason
  if (interruptReason === 'process_interrupted') return '上次执行中断，未自动续跑'
  const value = record.display_error ?? (Object.hasOwn(record, 'error') ? record.error : result.error)
  return typeof value === 'string' && value.trim() ? shortErrorReason(new Error(value)) : ''
}
