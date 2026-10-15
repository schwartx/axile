import { ErrorCopyButton } from '@/components/ui/ErrorDetailsContent'
import type { ConnectionTechnicalInfo, TechnicalSection } from '@/features/account/connectionTechnicalDetails'

function TechnicalSectionContent({ section }: { section: TechnicalSection }) {
  return <section className="min-w-0 space-y-2 border-t border-line pt-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-[12px] font-medium text-ink-2">{section.label}</h3>
      {section.kind === 'stack' && <ErrorCopyButton text={section.value} label={`复制${section.label}`} />}
    </div>
    {section.key === 'reporting_stack' && <p className="text-[12px] text-ink-3">记录上报时的 Python 调用路径，用于定位上报位置。</p>}
    {section.kind === 'text' ? <p className="whitespace-pre-wrap [overflow-wrap:anywhere] text-ink-1">{section.value}</p>
      : <pre tabIndex={0} aria-label={section.label}
        className="max-w-full overflow-x-auto rounded-lg bg-bg-subtle p-3 whitespace-pre font-mono text-[12px] leading-relaxed text-ink-2 focus-visible:outline-2 focus-visible:outline-accent">{section.value}</pre>}
  </section>
}

export function ConnectionTechnicalContent({ info }: { info: ConnectionTechnicalInfo }) {
  return <div className="min-w-0 space-y-3 text-[13px]">
    {info.message && <p className="whitespace-pre-wrap [overflow-wrap:anywhere] text-ink-1">{info.message}</p>}
    {!!info.fields.length && <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2">{info.fields.map(field => <div key={field.key} className="contents">
      <dt className="text-[12px] text-ink-3">{field.label}</dt>
      <dd className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] text-ink-2">{field.value}</dd>
    </div>)}</dl>}
    {info.stackNotice && <p className="border-l-2 border-line pl-3 text-[12px] text-ink-3">{info.stackNotice}</p>}
    {info.sections.map(section => <TechnicalSectionContent key={section.key} section={section} />)}
  </div>
}
