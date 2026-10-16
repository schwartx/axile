import { useState } from 'react'
import { ErrorDetailsModal } from '@/components/ui/ErrorDetailsModal'
import { assetRefreshFeedback } from '@/features/account/assetRefreshModel'
import { errorInfo } from '@/lib/errorInfo'
import { useErrorPresentation } from '@/lib/errorPresentation'

/** 自动与手动刷新共用资产入口，保留原始错误而不另起警告区块。 */
export function AssetRefreshFeedback({ accountId, manualError, automaticError }: {
  accountId: number; manualError: unknown; automaticError: unknown
}) {
  const { error, busy } = assetRefreshFeedback(manualError, automaticError)
  const { local } = useErrorPresentation(error, { intent: 'mutation', networkFeedback: 'global-only' })
  const [selectedError, setSelectedError] = useState<unknown>(null)
  const label = busy ? '账户忙碌' : '刷新失败'
  return <>
    {local && <button type="button" aria-label={`查看资产刷新详情：${label}`} onClick={() => setSelectedError(error)}
      className={`min-h-6 shrink-0 cursor-pointer hover:underline focus-visible:outline-2 focus-visible:outline-accent ${busy ? 'text-ink-3' : 'text-warn'}`}>{label}</button>}
    <ErrorDetailsModal selection={local && selectedError === error ? { title: '资产刷新详情', info: errorInfo(error, [{ label: '账户', value: String(accountId) }]) } : null} onClose={() => setSelectedError(null)} />
  </>
}
