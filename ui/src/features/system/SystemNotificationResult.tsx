import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Link } from '@/components/ui/nav'
import { getSystemNotificationResult } from '@/lib/api/init'
import { usePolling } from '@/lib/hooks/usePolling'

export function SystemNotificationResult() {
  const state = usePolling(getSystemNotificationResult, { queryKey: 'system:notificationResult', intervalMs: 10000 })
  const result = state.data
  return (
    <div className="text-[13px] leading-6 text-ink-2">
      <ErrorNotice intent="read" title="告警结果读取失败" hasData={state.data != null} error={state.error} onRetry={state.refresh} />
      {(!state.error || result != null) && (!result ? <p>读取告警结果中…</p> : !result.finished_at ? <p>尚无真实告警发送记录</p> : <>
        <p className={result.ok ? 'text-ink-2' : 'text-warn'}>{result.ok ? '发送成功' : '发送失败'} · {result.mode === 'function' ? '自定义函数' : '默认飞书'} · {result.event_type === 'execution_timeout' ? '执行超时' : '执行异常'}</p>
        <p className="break-all">{result.finished_at}</p>
        {result.account_id != null && <Link to={`/accounts/${result.account_id}`} className="text-accent">账户 #{result.account_id}</Link>}
        {result.execution_id && <p className="break-all">执行：{result.execution_id}</p>}
        {result.error && <p className="break-words whitespace-pre-wrap text-warn">{result.error}</p>}
      </>)}
    </div>
  )
}
