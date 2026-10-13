import { useEffect, useRef, useState } from 'react'
import { Check, Circle, LoaderCircle, RadioTower, TriangleAlert } from 'lucide-react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { ConnectionValidationErrorModal, type ValidationErrorSelection } from '@/features/account/connectionValidation/ConnectionValidationErrorModal'
import { ValidationStepDetailsModal } from '@/features/account/connectionValidation/ValidationStepDetailsModal'
import { ConnectionValidationSession } from '@/features/account/connectionValidation/connectionValidationSession'
import { connectionValidationApi, connectionValidationApiForAccount, validationRunning, type ConnectionValidationJob, type ValidationStepStatus, type ValidationStepDetails } from '@/lib/api/connectionValidation'
import type { ChannelCapability } from '@/types/api'

const stepLabels: Record<ValidationStepStatus, string> = {
  pending: '待检查', running: '检查中', passed: '已通过', failed: '失败', skipped: '未执行',
}

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

/** 错误文本常驻，收起时保留旧内容，让下方行随布局自然移动。 */
function ValidationMessage({ message, inset = false, onDetails }: { message?: string | null; inset?: boolean; onDetails?: () => void }) {
  const lastMessage = useRef(message)
  if (message) lastMessage.current = message
  return (
    <div inert={!message} aria-hidden={!message} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${message ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden">
        <p role={message ? 'alert' : undefined} className={`pt-1 text-[12px] leading-relaxed text-warn ${inset ? 'pl-[22px]' : ''}`}>
          {lastMessage.current}
          {onDetails && <button type="button" onClick={onDetails} className="ml-2 inline cursor-pointer whitespace-nowrap text-[12px] text-accent underline-offset-4 hover:underline">查看错误详情</button>}
        </p>
      </div>
    </div>
  )
}

/** 图标在固定槽内交接，运行圆环只在该阶段真实执行时挂载。 */
function ValidationStepRow({ step, busy, onDetails, onStepDetails }: {
  step: ConnectionValidationJob['steps'][number]
  busy: boolean
  onDetails: () => void
  onStepDetails: (record: ValidationStepDetails) => void
}) {
  const running = step.status === 'running' && busy
  const failed = step.status === 'failed'
  const passed = step.status === 'passed'
  const idle = !running && !failed && !passed
  const tone = failed ? 'text-warn' : running || passed ? 'text-ink-1' : 'text-ink-3'
  const iconClass = 'absolute transition-opacity duration-200 motion-reduce:transition-none'
  return (
    <li data-validation-step={step.id} data-status={step.status} className="py-2">
      <div className={`flex items-start justify-between gap-3 text-[13px] transition-colors duration-200 motion-reduce:transition-none ${tone}`}>
        <span className="flex min-w-0 items-start gap-2 leading-5">
          <span className="relative flex h-5 w-3.5 shrink-0 items-center justify-center" aria-hidden="true">
            <Circle size={10} className={`${iconClass} ${idle ? 'opacity-100' : 'opacity-0'}`} />
            <Check size={14} className={`${iconClass} ${passed ? 'opacity-100' : 'opacity-0'}`} />
            <TriangleAlert size={14} className={`${iconClass} ${failed ? 'opacity-100' : 'opacity-0'}`} />
            {running && <LoaderCircle size={14} className="absolute animate-spin motion-reduce:animate-none" />}
          </span>
          {step.label}
        </span>
        <span className="flex shrink-0 items-center gap-2 text-right text-[12px] leading-5">
          <InkRewrite text={stepLabels[step.status]} textClassName={tone} />
          <span inert={!step.details} aria-hidden={!step.details}
            className={`grid transition-[grid-template-columns] duration-200 motion-reduce:transition-none ${step.details ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]'}`}>
            <span className="min-w-0 overflow-hidden">
              <button type="button" aria-haspopup="dialog" onClick={() => { if (step.details) onStepDetails(step.details) }}
                className="cursor-pointer whitespace-nowrap text-accent underline-offset-4 hover:underline">查看详情</button>
            </span>
          </span>
        </span>
      </div>
      <ValidationMessage message={step.error?.message} inset onDetails={step.error ? onDetails : undefined} />
    </li>
  )
}

/** 检查项由渠道描述提供；组件不依赖 CTP 的阶段标识或数量。 */
export function ConnectionValidation({ descriptor, config, accountId }: {
  descriptor: ChannelCapability
  config: Record<string, unknown>
  accountId?: number
}) {
  const [job, setJob] = useState<ConnectionValidationJob | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [selectedError, setSelectedError] = useState<ValidationErrorSelection | null>(null)
  const [selectedDetails, setSelectedDetails] = useState<ValidationStepDetails | null>(null)
  const session = useRef<ConnectionValidationSession | null>(null)
  // 配置只在内存中比较，不进入 React key、DOM、URL 或持久化存储。
  const configVersion = JSON.stringify(config)
  const latestVersion = useRef(configVersion)
  latestVersion.current = configVersion
  const [resultVersion, setResultVersion] = useState(configVersion)

  useEffect(() => {
    session.current?.dispose()
    session.current = null
    setJob(null)
    setSelectedError(null)
    setSelectedDetails(null)
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
  const steps = shownJob?.steps ?? declaration.steps.map((step) => ({ ...step, status: 'pending' as const, error: null }))

  const start = () => {
    if (busy || !enabled) return
    session.current?.dispose()
    setJob(null)
    setSelectedError(null)
    setSelectedDetails(null)
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
            <InkRewrite tone="prose" text={shownError ? '验证未完成' : summary(shownJob, current && starting)} textClassName={shownError || shownJob?.status === 'failed' ? 'text-warn' : 'text-ink-3'} />
          </div>
        </div>
        <button type="button" onClick={start} disabled={!enabled || busy} className="relative flex h-[34px] w-[100px] shrink-0 cursor-pointer items-center justify-center rounded-lg bg-ink-1 px-2 text-[13px] font-[550] text-surface transition-opacity duration-200 motion-reduce:transition-none disabled:cursor-not-allowed disabled:opacity-45">
          {current && starting && <LoaderCircle size={12} className="absolute left-1 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
          <InkRewrite text={busy ? '验证中…' : shownJob || shownError ? '重新验证' : '验证连接'} />
        </button>
      </div>
      <ol aria-label="连接检查步骤" aria-busy={busy} className="mt-3 border-t border-line pt-1">
        {steps.map((step) => <ValidationStepRow key={step.id} step={step} busy={busy} onStepDetails={setSelectedDetails} onDetails={() => {
          if (step.error) setSelectedError({ stage: step.label, error: step.error })
        }} />)}
      </ol>
      <ValidationMessage message={shownError?.message ?? (
        shownJob?.error && !shownJob.steps.some((step) => step.error) ? shownJob.error.message : null
      )} onDetails={shownError || shownJob?.error ? () => setSelectedError({
        stage: '连接验证', error: shownJob?.error ?? { reason_code: 'CONNECTION.REQUEST_FAILED', message: shownError!.message },
      }) : undefined} />
      <div>
        {!enabled && <p className="mt-2 text-[12px] text-warn">当前服务器缺少此渠道依赖，安装后才能验证连接。</p>}
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
      <ValidationStepDetailsModal details={current ? selectedDetails : null} onClose={() => setSelectedDetails(null)} />
      <ConnectionValidationErrorModal selection={selectedError} onClose={() => setSelectedError(null)} />
    </section>
  )
}
