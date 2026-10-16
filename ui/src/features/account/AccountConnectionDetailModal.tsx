import { ConnectionProgressView } from '@/features/account/connectionProgress/ConnectionProgressView'
import { useLiveConnection } from '@/stores/liveConnection'
import { useId, useState, type ReactNode } from 'react'
import { OverflowText } from '@/components/ui/OverflowText'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { ErrorCopyButton, ErrorDetailsContent } from '@/components/ui/ErrorDetailsContent'
import { Link } from '@/components/ui/nav'
import type { AccountConnectionDetail } from '@/types/api'
import { connectionProblem } from '@/features/account/connectionProblem'
import { ConnectionTechnicalContent } from '@/features/account/ConnectionTechnicalContent'
import { AccountConnectionActions } from '@/features/account/AccountConnectionActions'

import { useConnectionFeedback } from '@/features/account/useConnectionFeedback'
import { errorInfo } from '@/lib/errorInfo'

export type ConnectionSelection = { accountId: number; requestId: number; detail: AccountConnectionDetail }

function TechnicalDetails({ children, label = '技术详情', preview }: { children: ReactNode; label?: string; preview?: ReactNode }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return <section className="min-w-0 border-t border-line py-3">
    <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(value => !value)}
      className="flex w-full cursor-pointer items-baseline gap-2 text-left text-[13px] text-ink-2 focus-visible:outline-2 focus-visible:outline-accent">
      <span aria-hidden="true">{open ? '▾' : '▸'}</span> <span className="shrink-0">{label}</span>{preview}
    </button>
    <div id={id} inert={!open} aria-hidden={!open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'visible grid-rows-[1fr]' : 'invisible grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden"><div role="region" aria-label={label === '技术详情' ? '完整技术详情' : label} className="mt-3">{children}</div></div>
    </div>
  </section>
}

/** 展示共享实时状态；渠道提供内容，公共 UI 只解析通用导航意图。 */
export function AccountConnectionDetailModal({ selection, onClose }: {
  selection: ConnectionSelection | null; onClose: () => void
}) {
  const synced = useLiveConnection(state => state.connected)
  const feedback = useConnectionFeedback(selection?.accountId ?? -1)
  const view = selection ? connectionProblem(selection.accountId, selection.detail) : null
  const operationInfo = feedback.error ? errorInfo(feedback.error) : null
  if (operationInfo) operationInfo.evidence = operationInfo.evidence.filter(field => field.label !== 'HTTP')
  const messages = [...new Set([view?.title, view?.summary].map(message => message?.trim()))]
    .filter(message => message && message !== operationInfo?.message.trim())
  const copy = [view?.copy, operationInfo?.text].filter(Boolean).join('\n\n')
  const footerActions = <>
    {copy && <ErrorCopyButton text={copy} label="复制连接详情" variant="text" />}
    <button type="button" aria-label="关闭详情" onClick={onClose} className="cursor-pointer rounded-lg border border-line px-3 py-2 text-[13px] text-ink-2">关闭</button>
  </>
  return <CenteredModal open={selection !== null} title="账户连接详情" onClose={onClose} widthClassName="w-[640px]"
    footer={selection
      ? <AccountConnectionActions key={selection.accountId} accountId={selection.accountId} detail={selection.detail}>{footerActions}</AccountConnectionActions>
      : <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line px-[22px] py-3">{footerActions}</div>}>
    <div className="min-w-0 space-y-4 text-[13px] leading-relaxed [overflow-wrap:anywhere]">
      {operationInfo && <section aria-label="连接操作结果" className="min-w-0">
        <ErrorDetailsContent info={operationInfo} />
      </section>}
      {view && <>
        <p className="whitespace-pre-wrap text-[14px] text-ink-2">{messages.join('\n')}</p>
        {selection?.detail.progress && <ConnectionProgressView key={selection.detail.progress.run_id}
          progress={selection.detail.progress} active={synced && !['unknown', 'disconnected'].includes(selection.detail.status)}
          source={{ kind: 'account', accountId: selection.accountId, runId: selection.detail.progress.run_id }} /> }
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-5 gap-y-2 text-ink-2">{view.facts.map(field => <div key={field.label} className="contents">
          <dt className="text-[12px] text-ink-3">{field.label}</dt><dd className="min-w-0 tabular-nums">{field.value}</dd>
        </div>)}</dl>
        {view.sections.map((section, i) => <section key={i} className="space-y-2 border-t border-line pt-4">
          {section.title && <h3 className="font-medium text-ink-2">{section.title}</h3>}
          {section.text && <p className="whitespace-pre-wrap">{section.text}</p>}
          {!!section.fields?.length && <dl className="space-y-2">{section.fields.map((field, j) => <div key={j} className="flex flex-wrap gap-x-3">
            <dt className="text-ink-3">{field.label}</dt><dd className="min-w-0 whitespace-pre-wrap">{field.value}</dd>
          </div>)}</dl>}
          {!!section.steps?.length && <ol className="list-decimal space-y-2 pl-5">{section.steps.map((step, j) => <li key={j} className="whitespace-pre-wrap">{step}</li>)}</ol>}
        </section>)}
        {!!view.actions.length && <div className="flex flex-wrap gap-3">{view.actions.map((action, i) =>
          <Link key={i} to={`/accounts/${selection!.accountId}/edit/connection`} onClick={onClose}
            className="rounded-lg border border-line px-3 py-2 text-accent focus-visible:outline-2 focus-visible:outline-accent">{action.label}</Link>)}</div>}
        {view.history && <TechnicalDetails label="历史连接失败" preview={<><OverflowText className="min-w-0 flex-1 text-ink-3" text={view.history.message ?? ''} /><time className="ml-auto shrink-0 text-[12px] tabular-nums text-ink-3">{view.history.observed_at.replace('T', ' ').slice(5, 16)}</time></>}>
          <p className="mb-2 text-ink-2">{view.history.observed_at.replace('T', ' ').replace(/(\d{2}:\d{2}:\d{2})\.\d+/, '$1')}</p>
          <p className="text-ink-3">这条记录保留用于排查，不代表当前会话仍在故障。</p>
          <p className="whitespace-pre-wrap">{view.history.message}</p>
         {view.history.technicalInfo && <TechnicalDetails><ConnectionTechnicalContent info={view.history.technicalInfo} /></TechnicalDetails>}
        </TechnicalDetails>}
        {view.technicalInfo && <TechnicalDetails key={selection!.requestId}><ConnectionTechnicalContent info={view.technicalInfo} /></TechnicalDetails>}
        <p className="pb-2 text-[12px] text-ink-3">连接后会核对资产和委托，交易任务需另行执行。</p>
      </>}
    </div>
  </CenteredModal>
}
