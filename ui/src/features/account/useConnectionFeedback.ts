import { useErrorPresentation } from '@/lib/errorPresentation'
import { shortErrorReason } from '@/lib/errorInfo'
import { useAccountConnectionSnapshot } from '@/stores/liveConnection'
import { useConnectionOperation } from '@/stores/connectionOperations'

/** 原始操作结果保留；新就绪快照只解除已恢复的连接失败，不误清断开失败。 */
export function connectionFeedback(operation: ReturnType<typeof useConnectionOperation>, connection: ReturnType<typeof useAccountConnectionSnapshot>, local: boolean) {
  const recovered = operation.action !== 'disconnect' && operation.failedFrame !== undefined && operation.failedFrame?.status !== 'ready' && operation.failedFrame !== connection && connection.status === 'ready'
  const error = local && !recovered ? operation.error : null
  // 标识直接说明本次失败原因；全文交给 OverflowText，不在数据层截断。
  const reason = error ? shortErrorReason(error, Infinity) : ''
  const fallback = operation.action === 'disconnect' ? '断开失败' : '连接失败'
  const label = error ? (reason && reason !== '未知错误' ? reason : fallback) : null
  return { error, label }
}

// 多个入口共享一次操作的展示归属，已恢复的旧错误不随下一次断线复活。
const recoveredOperations = new WeakSet<object>()

/** 操作错误只归连接入口，共用全局网络归属与恢复判断。 */
export function useConnectionFeedback(accountId: number) {
  const operation = useConnectionOperation(accountId)
  const connection = useAccountConnectionSnapshot(accountId)
  const { local } = useErrorPresentation(operation.error, { intent: 'mutation', networkFeedback: 'global-only' })
  const feedback = connectionFeedback(operation, connection, local)
  if (local && operation.error != null && feedback.error === null) recoveredOperations.add(operation)
  return recoveredOperations.has(operation) ? { error: null, label: null } : feedback
}
