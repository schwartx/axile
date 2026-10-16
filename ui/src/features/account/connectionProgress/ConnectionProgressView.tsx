import { useRef, useState } from 'react'
import { Check, Circle, LoaderCircle, TriangleAlert } from 'lucide-react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { shortErrorReason } from '@/lib/errorInfo'
import { connectionCompletion, isConnectionTable, type ConnectionRun, type ConnectionStep, type ConnectionStepStatus, type ConnectionKeyValueDetails, type ConnectionProgressSource } from '@/lib/api/connectionProgress'
import { ConnectionStepDetailsModal } from '@/features/account/connectionProgress/ConnectionStepDetailsModal'
import { ConnectionStepErrorModal, type ConnectionErrorSelection } from '@/features/account/connectionProgress/ConnectionStepErrorModal'
import { ConnectionRecordsModal, type ConnectionTableSelection } from '@/features/account/connectionProgress/ConnectionRecordsModal'

const stepLabels: Record<ConnectionStepStatus, string> = { pending: '待检查', running: '进行中', passed: '已完成', failed: '失败', skipped: '未执行' }

/** 错误文本常驻，收起时保留旧内容，让下方行随布局自然移动。 */
export function ConnectionMessage({ message, inset = false, onDetails }: { message?: string | null; inset?: boolean; onDetails?: () => void }) {
  const lastMessage = useRef(message)
  if (message) lastMessage.current = message
  return (
    <div inert={!message} aria-hidden={!message} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${message ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden">
        <p role={message ? 'alert' : undefined} className={`pt-1 text-[12px] leading-relaxed text-warn ${inset ? 'pl-[22px]' : ''}`}>
          {lastMessage.current ? shortErrorReason(lastMessage.current) : null}
          {onDetails && <button type="button" onClick={onDetails} className="ml-2 inline cursor-pointer whitespace-nowrap text-[12px] text-accent underline-offset-4 hover:underline">查看错误详情</button>}
        </p>
      </div>
    </div>
  )
}

/** 图标在固定槽内交接，运行圆环只在该阶段真实执行时挂载。 */
function ConnectionStepRow({ step, busy, onDetails, onStepDetails }: {
  step: ConnectionStep
  busy: boolean
  onDetails: () => void
  onStepDetails: () => void
}) {
  const running = step.status === 'running' && busy
  const failed = step.status === 'failed'
  const passed = step.status === 'passed'
  const idle = !running && !failed && !passed
  const elapsed = step.started_at && step.finished_at ? (Date.parse(step.finished_at) - Date.parse(step.started_at)) / 1000 : null
  const duration = elapsed !== null && Number.isFinite(elapsed) && elapsed >= 0 ? `${elapsed < 10 ? elapsed.toFixed(1) : Math.round(elapsed)}s` : null
  const tone = failed ? 'text-warn' : running || passed ? 'text-ink-1' : 'text-ink-3'
  const iconClass = 'absolute transition-opacity duration-200 motion-reduce:transition-none'
  const tableSummary = step.details && isConnectionTable(step.details)
    ? step.details.summary.replace(/^(\d+\s*[条个])(?:订单|成交|持仓记录|(?:螺纹钢)?合约)$/, '$1')
    : ''
  return (
    <li data-connection-step={step.id} data-status={step.status} className="py-1">
      <div className={`grid grid-cols-[minmax(0,1fr)_36px_48px] items-start gap-x-2 text-[13px] transition-colors duration-200 motion-reduce:transition-none ${tone}`}>
        <span className="flex min-w-0 items-start gap-2 leading-5">
          <span className="relative flex h-5 w-3.5 shrink-0 items-center justify-center" aria-hidden="true">
            <Circle size={10} className={`${iconClass} ${idle ? 'opacity-100' : 'opacity-0'}`} />
            <Check size={14} className={`${iconClass} ${passed ? 'opacity-100' : 'opacity-0'}`} />
            <TriangleAlert size={14} className={`${iconClass} ${failed ? 'opacity-100' : 'opacity-0'}`} />
            {running && <LoaderCircle size={14} className="absolute animate-spin motion-reduce:animate-none" />}
          </span>
          <span className="flex min-w-0 flex-wrap items-baseline gap-x-1">
            <span>{step.label}</span>
            {duration && <span className="text-[11px] tabular-nums text-ink-3">· {duration}</span>}
            <span inert={!tableSummary} aria-hidden={!tableSummary}
              className={`grid transition-[grid-template-columns] duration-200 motion-reduce:transition-none ${tableSummary ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]'}`}>
              <span className="min-w-0 overflow-hidden text-[11px] leading-4 text-ink-3">
                <InkRewrite text={tableSummary ? `· ${tableSummary}` : ''} />
              </span>
            </span>
          </span>
        </span>
        <span className="text-right text-[12px] leading-5">
          <InkRewrite text={step.status === 'running' && !busy ? '待确认' : stepLabels[step.status]} textClassName={tone} />
        </span>
        <span inert={!step.details} aria-hidden={!step.details}
          className={`text-right text-[12px] leading-5 transition-opacity duration-200 motion-reduce:transition-none ${step.details ? 'opacity-100' : 'opacity-0'}`}>
          <button type="button" aria-haspopup="dialog" onClick={() => { if (step.details) onStepDetails() }}
            className="cursor-pointer whitespace-nowrap text-accent underline-offset-4 hover:underline">查看详情</button>
        </span>
      </div>
      <ConnectionMessage message={step.error?.message} inset onDetails={step.error ? onDetails : undefined} />
    </li>
  )
}


export function ConnectionProgressBar({ progress, compact = false }: {
  progress: Pick<ConnectionRun, 'steps' | 'status'>; compact?: boolean
}) {
  const { completed, total, percent } = connectionCompletion(progress)
  return <div className={compact ? 'w-full' : 'mb-3 mt-3'}>
    {!compact && <p className="mb-1 text-[11px] tabular-nums text-ink-3">已完成 {completed} / {total} 步</p>}
    <div role="progressbar" aria-label="账户连接进度" aria-valuemin={0} aria-valuemax={total || 1}
      aria-valuenow={completed} aria-valuetext={`已完成 ${completed} / ${total} 步`}
      className={`overflow-hidden rounded-full bg-fill ${compact ? 'h-0.5' : 'h-1'}`}>
      <div className={`h-full rounded-full transition-[width] duration-200 motion-reduce:transition-none ${progress.status === 'failed' ? 'bg-warn' : 'bg-ink-3'}`}
        style={{ width: `${percent}%` }} />
    </div>
  </div>
}

/** 同一套步骤用于常驻连接和临时验证；数据源只影响表格的按需读取。 */
export function ConnectionProgressView({ progress, source, active = true }: {
  progress: Pick<ConnectionRun, 'steps' | 'status'>
  source?: ConnectionProgressSource
  active?: boolean
}) {
  const [selectedError, setSelectedError] = useState<ConnectionErrorSelection | null>(null)
  const [selectedDetails, setSelectedDetails] = useState<ConnectionKeyValueDetails | null>(null)
  const [selectedTable, setSelectedTable] = useState<ConnectionTableSelection | null>(null)
  const busy = active && progress.status === 'running'
  return <>
    <ConnectionProgressBar progress={progress} />
    <ol aria-label="连接检查步骤" aria-busy={busy} className="border-t border-line pt-1">
      {progress.steps.map(step => <ConnectionStepRow key={step.id} step={step} busy={busy}
        onDetails={() => { if (step.error) setSelectedError({ stage: step.label, error: step.error }) }}
        onStepDetails={() => {
          if (!step.details) return
          if (isConnectionTable(step.details)) {
            if (source) setSelectedTable({ source, stepId: step.id, details: step.details })
          } else setSelectedDetails(step.details)
        }} />)}
    </ol>
    <ConnectionRecordsModal selection={selectedTable} onClose={() => setSelectedTable(null)} />
    <ConnectionStepDetailsModal details={selectedDetails} onClose={() => setSelectedDetails(null)} />
    <ConnectionStepErrorModal selection={selectedError} onClose={() => setSelectedError(null)} />
  </>
}
