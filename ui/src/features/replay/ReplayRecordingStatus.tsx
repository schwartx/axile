import { useCallback } from 'react'
import { getReplayStatus } from '@/lib/api/replay'
import { usePolling } from '@/lib/hooks/usePolling'
import { ErrorNotice } from '@/components/ui/ErrorNotice'

const labels: Record<string, string> = { disabled: '已关闭', waiting_connection: '已开启，等待账户正常连接', starting: '录制设置待生效', recording: '正在记录', closing: '正在结束本段记录', error: '录制状态异常', unsupported: '当前渠道暂不支持录制' }

/** 保存意愿与 worker 实际生效分开，录制状态不会触发连接或交易。 */
export function ReplayRecordingStatus({ accountId, savedEnabled }: { accountId: number; savedEnabled: boolean }) {
  const fetcher = useCallback((signal: AbortSignal) => getReplayStatus(accountId, signal), [accountId])
  const result = usePolling(fetcher, { queryKey: `replay:status:${accountId}:${savedEnabled}`, intervalMs: 2500, enabled: Number.isSafeInteger(accountId) && accountId > 0 })
  const state = result.data
  const pending = state && state.effective_version !== state.config_version
  return <div className="text-[12px] leading-5 text-ink-2">
    <span role="status">{state ? labels[state.state] ?? '状态未知' : result.loading ? '正在读取实际状态…' : '实际状态未知'}{pending ? ' · 设置尚待生效' : ''}</span>
    {(state?.reason || state?.error) && <p className="text-warn">{state.reason ?? state.error}</p>}
    <ErrorNotice intent="read" variant="compact" title="录制状态读取失败" error={result.error} contextKey={accountId} onRetry={result.refresh} />
  </div>
}
