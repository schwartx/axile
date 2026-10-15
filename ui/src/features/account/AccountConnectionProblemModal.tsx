import { useId, useState } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { ErrorCopyButton } from '@/components/ui/ErrorDetailsContent'
import { Link } from '@/components/ui/nav'
import type { ErrorInfo } from '@/lib/errorInfo'
import type { AccountConnectionDetail } from '@/types/api'
import { connectionProblem } from '@/features/account/connectionProblem'

export type ConnectionSelection = { accountId: number; requestId: number } & (
  { kind: 'loading' } | { kind: 'detail'; detail: AccountConnectionDetail } | { kind: 'failure'; info: ErrorInfo }
)

function TechnicalDetails({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  if (!text) return null
  return <section className="min-w-0 border-t border-line py-4">
    <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(value => !value)}
      className="cursor-pointer text-[13px] text-ink-2 focus-visible:outline-2 focus-visible:outline-accent">
      <span aria-hidden="true">{open ? '▾' : '▸'}</span> 技术详情
    </button>
    <div id={id} inert={!open} aria-hidden={!open} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'visible grid-rows-[1fr]' : 'invisible grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden"><pre tabIndex={0} aria-label="完整技术详情"
        className="mt-3 max-w-full overflow-x-auto whitespace-pre font-mono text-[12px] leading-relaxed focus-visible:outline-2 focus-visible:outline-accent">{text}</pre></div>
    </div>
  </section>
}

/** 只展示本次读取快照；渠道提供内容，公共 UI 只解析通用导航意图。 */
export function AccountConnectionProblemModal({ selection, onClose }: { selection: ConnectionSelection | null; onClose: () => void }) {
  const view = selection?.kind === 'detail' ? connectionProblem(selection.accountId, selection.detail) : null
  const title = selection?.kind === 'failure' ? '连接详情读取失败' : view?.title ?? '账户连接详情'
  const copy = selection?.kind === 'failure' ? selection.info.text : view?.copy
  return <CenteredModal open={selection !== null} title={title} onClose={onClose} widthClassName="w-[640px]"
    footer={<div className="flex flex-wrap items-center justify-end gap-3 border-t border-line px-[22px] py-4">
      {copy && <ErrorCopyButton text={copy} label="复制连接详情" />}
      <button type="button" onClick={onClose} className="cursor-pointer rounded-lg bg-ink-1 px-4 py-2 text-[13px] text-surface">关闭详情</button>
    </div>}>
    <div className="min-w-0 space-y-4 text-[13px] leading-relaxed [overflow-wrap:anywhere]">
      {selection?.kind === 'loading' && <p role="status" className="py-4 text-ink-3">正在读取连接详情…</p>}
      {selection?.kind === 'failure' && <>
        <p>未能读取账户 {selection.accountId} 的连接详情。关闭后再次打开可重新读取。</p>
        <TechnicalDetails key={selection.requestId} text={selection.info.text} />
      </>}
      {view && <>
        <p className="whitespace-pre-wrap text-[15px]">{view.summary}</p>
        <dl className="flex flex-wrap gap-x-6 gap-y-2 text-ink-2">{view.facts.map(field => <div key={field.label}>
          <dt className="text-[12px] text-ink-3">{field.label}</dt><dd>{field.value}</dd>
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
        <TechnicalDetails key={selection!.requestId} text={view.technical} />
      </>}
    </div>
  </CenteredModal>
}
