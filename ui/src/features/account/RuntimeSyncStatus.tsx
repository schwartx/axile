import { useRef } from 'react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useAccountRuntimeSync } from '@/features/account/useAccountRuntimeSync'
import { ApiError } from '@/lib/api/client'

/** 保存结果和运行态分别表达；查询与重试同步使用各自接口。 */
export function RuntimeSyncStatus({ runtime, saved = false, disabled = false, accountId }: {
  runtime: ReturnType<typeof useAccountRuntimeSync>; saved?: boolean; disabled?: boolean; accountId: number
}) {
  const sync = runtime.data
  const unresolved = runtime.queried && sync?.status !== 'synchronized'
  const observed = useRef({ accountId, unresolved: false })
  if (observed.current.accountId !== accountId) observed.current = { accountId, unresolved: false }
  if (unresolved) observed.current.unresolved = true
  const failure = sync?.last_error ? new ApiError(202, sync.last_error, { body: sync }) : null
  return <div>
    {(unresolved || saved || observed.current.unresolved) && <div className="mt-2 text-[13px] text-ink-2" aria-live="polite">
      <span>{sync?.status === 'synchronized' ? '配置已保存，运行态已同步' : sync?.status === 'failed' ? '配置已保存，运行态同步失败' : '配置已保存，运行态待同步'}</span>
      {sync && <span className="ml-2 text-ink-3">revision {sync.revision} · 尝试 {sync.attempts} 次</span>}
    </div>}
    <ErrorNotice evidence={[{ label: '账户', value: String(accountId) }]} title="运行态同步未完成" error={unresolved ? failure : null} contextKey={accountId} />
    <ErrorNotice evidence={[{ label: '账户', value: String(accountId) }]} title="运行态状态查询失败" error={runtime.error} contextKey={accountId} />
    {(unresolved || runtime.error) && <div className="mt-2 flex gap-3 text-[13px]">
      <button type="button" disabled={disabled || runtime.loading} onClick={() => void runtime.refresh()} className="cursor-pointer text-accent disabled:opacity-40">重新查询</button>
      <button type="button" disabled={disabled || runtime.loading} onClick={() => void runtime.retry()} className="cursor-pointer text-accent disabled:opacity-40">重试同步</button>
    </div>}
  </div>
}
