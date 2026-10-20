import { getExecuteRecordDetail, getExecuteRecords } from '@/lib/api/accounts'
import type { ExecuteRecord } from '@/types/api'

interface Query { recordId?: number | null; executionId?: string | null }
interface Readers {
  list: typeof getExecuteRecords
  detail: typeof getExecuteRecordDetail
}

/** 摘要只用于定位；错误证据读取选中记录的完整详情。 */
export async function readExecutionRecordEvidence(
  accountId: number, query: Query, signal: AbortSignal,
  readers: Readers = { list: getExecuteRecords, detail: getExecuteRecordDetail },
): Promise<ExecuteRecord | null> {
  signal.throwIfAborted()
  let recordId = query.recordId
  if (recordId == null) {
    if (!query.executionId) return null
    const page = await readers.list(accountId, { execution_id: query.executionId, limit: 1 }, signal)
    signal.throwIfAborted()
    recordId = page.data[0]?.id
    if (recordId == null) return null
  }
  const record = await readers.detail(accountId, recordId, signal)
  signal.throwIfAborted()
  if (record.account_id !== accountId || record.id !== recordId || query.executionId && record.execution_id !== query.executionId) {
    throw new Error('执行记录详情与选中记录不一致')
  }
  return record
}
