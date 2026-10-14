import { useState } from 'react'
import { Link } from '@/components/ui/nav'
import { ErrorDetailsModal } from '@/components/ui/ErrorDetailsModal'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { batchExecutionInfo, batchResultStatus, type BatchResult } from '@/features/portfolio/batchExecutionSnapshot'

/** 页面逐账户重试；汇总弹窗始终阅读、复制打开时的同一份结果。 */
export function BatchExecutionResults({ portfolioId, results, onRetry }: {
  portfolioId: number
  results: BatchResult[]
  onRetry: (accountId: number, name: string) => Promise<void>
}) {
  const [selection, setSelection] = useState<{ info: ReturnType<typeof batchExecutionInfo>; pending: boolean } | null>(null)
  return <div role="region" aria-label="批量执行受理结果">
    {results.length > 0 && <button type="button" className="mb-2 cursor-pointer text-[13px] text-accent hover:underline"
      onClick={() => setSelection({ info: batchExecutionInfo(portfolioId, results), pending: results.some(result => result.pending) })}>查看批量结果详情</button>}
    {results.map(result => <div key={result.accountId} className="mb-3 border-b border-line pb-2 text-[13px]">
      <div>{result.name} · {batchResultStatus(result)}</div>
      {result.trigger && <Link to={`/accounts/${result.accountId}/executions/${result.trigger.execution_id}`} className="break-all text-accent">{result.trigger.execution_id}</Link>}
      <ErrorNotice title={`${result.name} 执行请求失败`} error={result.error} evidence={[{ label: '账户', value: String(result.accountId) }]}
        onRetry={() => onRetry(result.accountId, result.name)} contextKey={portfolioId} />
    </div>)}
    <ErrorDetailsModal selection={selection ? { title: '批量执行受理结果', info: selection.info } : null}
      copyLabel={selection?.pending ? '复制当前结果' : '复制全部结果'} onClose={() => setSelection(null)} />
  </div>
}
