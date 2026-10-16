import { useEffect, useRef, useState } from 'react'
import { LoaderCircle, RadioTower } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { ApiError } from '@/lib/api/client'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { ConnectionValidationSession } from '@/features/account/connectionValidation/connectionValidationSession'
import { connectionValidationApi, connectionValidationApiForAccount, validationRunning, type ConnectionValidationJob } from '@/lib/api/connectionValidation'
import { ConnectionProgressView, ConnectionMessage } from '@/features/account/connectionProgress/ConnectionProgressView'
import { ConnectionStepErrorModal, type ConnectionErrorSelection } from '@/features/account/connectionProgress/ConnectionStepErrorModal'
import type { ChannelCapability } from '@/types/api'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'
import { useErrorPresentation } from '@/lib/errorPresentation'

function summary(job: ConnectionValidationJob | null, starting: boolean): string {
  if (starting) return '正在启动验证…'
  if (!job) return '尚未验证'
  switch (job.status) {
    case 'queued': return '等待验证'
    case 'running': return '正在验证连接…'
    case 'succeeded': return '账户连接验证通过'
    case 'failed': return '账户连接验证未通过'
    case 'cancelled': return '验证已取消'
  }
}

/** 检查项由渠道描述提供；组件不依赖 CTP 的阶段标识或数量。 */
export function ConnectionValidation({ descriptor, config, accountId }: {
  descriptor: ChannelCapability
  config: Record<string, unknown>
  accountId?: number
}) {
  const [selectedError, setSelectedError] = useState<ConnectionErrorSelection | null>(null)
  const [job, setJob] = useState<ConnectionValidationJob | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const session = useRef<ConnectionValidationSession | null>(null)
  // 配置只在内存中比较，不进入 React key、DOM、URL 或持久化存储。
  const configVersion = JSON.stringify(config)
  const latestVersion = useRef(configVersion)
  latestVersion.current = configVersion
  const [resultVersion, setResultVersion] = useState(configVersion)
  const requestFailure = useErrorPresentation(error, { intent: job ? 'uncertain' : 'mutation' })
  useReadRecovery(() => {
    if (resultVersion === configVersion && job && error) {
      setError(null)
      return session.current?.refresh()
    }
  })

  useEffect(() => {
    session.current?.dispose()
    session.current = null
    setJob(null)
    setSelectedError(null)
    setError(null)
    setStarting(false)
    setCancelling(false)
    setResultVersion(configVersion)
    return () => { session.current?.dispose() }
  }, [descriptor.channel, configVersion, accountId])

  const declaration = descriptor.connection_validation
  if (!declaration) return null
  const current = resultVersion === configVersion
  const shownJob = current ? job : null
  const shownError = current ? error : null
  const busy = current && (starting || (!error && !!job && validationRunning(job)))
  const enabled = descriptor.available
  const steps: ConnectionValidationJob["steps"] = shownJob?.steps ?? declaration.steps.map((step) => ({ ...step, status: 'pending' as const, error: null }))

  const start = () => {
    if (busy || !enabled) return
    session.current?.dispose()
    setJob(null)
    setSelectedError(null)
    setError(null)
    setStarting(true)
    setResultVersion(configVersion)
    const version = configVersion
    const next = new ConnectionValidationSession(
      (result) => {
        if (latestVersion.current !== version) return
        setJob(result)
        setStarting(false)
        setCancelling(false)
      },
      (failure) => {
        if (latestVersion.current !== version) return
        setError(failure)
        setStarting(false)
        setCancelling(false)
      },
      accountId == null ? connectionValidationApi : connectionValidationApiForAccount(accountId),
    )
    session.current = next
    void next.start(descriptor.channel, config)
  }

  return (
    <section aria-label="账户连接验证" className="rounded-[14px] border border-line bg-surface p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-[15px] font-[620] text-ink-1">
            <RadioTower size={16} className="shrink-0" aria-hidden="true" />连接验证
          </h2>
          <div aria-live="polite" className="mt-1 min-h-[18px] text-[12px]">
            <InkRewrite tone="prose" text={shownError ? '验证未完成' : summary(shownJob, current && starting)} textClassName={(shownError && requestFailure.local) || shownJob?.status === 'failed' ? 'text-warn' : 'text-ink-3'} />
          </div>
        </div>
        <button type="button" onClick={start} disabled={!enabled || busy} className="relative flex h-[34px] w-[100px] shrink-0 cursor-pointer items-center justify-center rounded-lg bg-ink-1 px-2 text-[13px] font-[550] text-surface transition-opacity duration-200 motion-reduce:transition-none disabled:cursor-not-allowed disabled:opacity-45">
          {current && starting && <LoaderCircle size={12} className="absolute left-1 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
          <InkRewrite text={busy ? '验证中…' : shownJob || shownError ? '重新验证' : '验证连接'} />
        </button>
      </div>
      <ConnectionProgressView key={shownJob?.run_id ?? 'pending'} progress={shownJob ?? { status: 'queued', steps }}
        source={shownJob ? { kind: 'validation', validationId: shownJob.validation_id } : undefined} />
      <ErrorNotice intent={shownJob ? "uncertain" : "mutation"} title="连接验证请求失败" error={shownError} contextKey={`${accountId}:${configVersion}`}
        onRetry={shownError instanceof ApiError && shownError.status === 404 || !shownJob ? start : () => { setError(null); void session.current?.refresh() }} />
      {shownError instanceof ApiError && shownError.status === 404 && <p className="text-[12px] text-warn">验证任务已过期，请重新验证。</p>}
      <ConnectionMessage message={shownJob?.error && !shownJob.steps.some((step) => step.error) ? shownJob.error.message : null}
        onDetails={shownJob?.error ? () => setSelectedError({ stage: '连接验证', error: shownJob.error! }) : undefined} />
      <div>
        {!descriptor.available && <p className="mt-2 text-[12px] text-warn">当前服务器缺少此渠道依赖，安装后才能验证连接。</p>}
        <div inert={!shownJob?.finished_at && !(busy && shownJob)} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${shownJob?.finished_at || (busy && shownJob) ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
          <div className="min-h-0 overflow-hidden">
            <div className="pt-2 text-[11px] leading-5 text-ink-3">
              {shownJob?.finished_at ? <InkRewrite text={`检查于 ${new Date(shownJob.finished_at).toLocaleString('zh-CN')}`} />
                : <button type="button" disabled={!busy || !shownJob || cancelling} className="cursor-pointer text-[12px] text-ink-2 disabled:opacity-45" onClick={() => {
                  setCancelling(true)
                  void session.current?.cancel()
                }}><InkRewrite text={cancelling ? '正在取消…' : '取消验证'} /></button>}
            </div>
          </div>
        </div>
      </div>
      <ConnectionStepErrorModal selection={current ? selectedError : null} onClose={() => setSelectedError(null)} />
    </section>
  )
}
