import { useState } from 'react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import type { ErrorEvidence, ErrorInfo } from '@/lib/errorInfo'

const detailLabels: Record<string, string> = {
  message: '错误信息',
  technical_detail: '技术详情',
  traceback: '调用栈',
  formatted_traceback: '调用栈',
  validation_errors: '校验详情',
  exception_type: '异常类型',
  error_scope: '错误范围',
  status: '错误码',
  status_code: '状态码',
  reason_code: '原因码',
  error_id: '错误码',
  return_code: '返回码',
}

function fieldLabel(label: string): string {
  return label.split(' · ').map((part) => detailLabels[part] ?? part).join(' · ')
}

function fieldValue(label: string, value: string): string {
  if (label.split(' · ').at(-1) === 'error_scope' && value === 'account_connection') return '账户连接'
  return /时间|确认/.test(label) ? value.replace(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?/, '$1 $2') : value
}

function detailKind({ label, value }: ErrorEvidence): 'metadata' | 'raw' | 'content' {
  const key = label.split(' · ').at(-1) ?? label
  if (/traceback|stack|调用栈/i.test(key)) return 'content'
  if (['message', '错误原文', '原始响应', '响应对象'].includes(key)) return 'raw'
  try {
    const parsed: unknown = JSON.parse(value)
    if (parsed !== null && typeof parsed === 'object') return 'raw'
  } catch { /* 普通文本按长度和字段语义排版。 */ }
  if (!['technical_detail', 'details', 'validation_errors'].includes(key) && value.length <= 120 && !value.includes('\n')) return 'metadata'
  return 'content'
}

function DetailText({ label, value }: ErrorEvidence) {
  return <pre tabIndex={0} aria-label={label} className="max-w-full overflow-x-auto whitespace-pre font-mono text-[13px] leading-relaxed text-ink-1 focus-visible:outline-2 focus-visible:outline-accent">{value}</pre>
}

function ContentSection({ label, value }: ErrorEvidence) {
  const [open, setOpen] = useState(true)
  const collapsible = /traceback|stack|调用栈/i.test(label)
  return <section className="min-w-0 border-t border-line py-3">
    {collapsible ? <button type="button" aria-expanded={open} onClick={() => setOpen((previous) => !previous)} className="mb-2 inline-flex cursor-pointer items-center gap-2 text-[12px] text-ink-2 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent">
      <span aria-hidden="true">{open ? '▾' : '▸'}</span>{fieldLabel(label)}
    </button> : <h3 className="mb-2 text-[12px] font-medium text-ink-3">{fieldLabel(label)}</h3>}
    <div className={`grid transition-[grid-template-rows] duration-150 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`} aria-hidden={!open} inert={!open}>
      <div className="min-h-0 overflow-hidden"><DetailText label={label} value={value} /></div>
    </div>
  </section>
}

function readableMessage(message: string): string {
  try {
    const parsed: unknown = JSON.parse(message)
    if (parsed && typeof parsed === 'object' && 'message' in parsed && typeof parsed.message === 'string') return parsed.message
  } catch { /* 非结构化摘要直接使用原文。 */ }
  return message
}

/** 可嵌入现有弹窗；正文和复制均使用冻结的同一份详情。 */
export function ErrorDetailsContent({ info }: { info: ErrorInfo }) {
  const [rawOpen, setRawOpen] = useState(true)
  const message = readableMessage(info.message)
  const seen = new Set([message])
  const details = info.details.filter(({ value }) => {
    if (seen.has(value)) return false
    seen.add(value)
    return true
  })
  const metadata = [...info.evidence, ...details.filter((entry) => detailKind(entry) === 'metadata')]
  const content = details.filter((entry) => detailKind(entry) === 'content')
  const raw = details.filter((entry) => detailKind(entry) === 'raw')
  return <div className="min-w-0 text-[13px]">
    <p className="mb-3 whitespace-pre-wrap [overflow-wrap:anywhere] text-[15px] leading-relaxed text-ink-1">{message}</p>
    <dl className="mb-4 flex flex-wrap gap-x-6 gap-y-1.5">{metadata.map(({ label, value }, index) => <div key={index} className="flex min-w-0 items-baseline gap-2">
      <dt className="shrink-0 text-ink-3">{fieldLabel(label)}</dt><dd className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-ink-2">{fieldValue(label, value)}</dd>
    </div>)}</dl>
    {content.map(({ label, value }, index) => <ContentSection key={`${label}-${index}`} label={label} value={value} />)}
    {raw.length > 0 && <section className="min-w-0 border-t border-line py-3">
      <button type="button" aria-expanded={rawOpen} onClick={() => setRawOpen((open) => !open)} className="inline-flex cursor-pointer items-center gap-2 text-[12px] text-ink-2 hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent">
        <span aria-hidden="true">{rawOpen ? '▾' : '▸'}</span>原始错误
      </button>
      <div className={`grid transition-[grid-template-rows] duration-150 motion-reduce:transition-none ${rawOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`} aria-hidden={!rawOpen} inert={!rawOpen}>
        <div className="min-h-0 overflow-hidden">{raw.map(({ label, value }, index) => <div key={index} className="min-w-0 pt-3">
          {raw.length > 1 && <h3 className="mb-1 text-[12px] text-ink-3">{fieldLabel(label)}</h3>}
          <DetailText label={label} value={value} />
        </div>)}</div>
      </div>
    </section>}
  </div>
}

export function ErrorCopyButton({ text, label = '复制错误信息', disabled = false, variant = 'outline' }: { text: string; label?: string; disabled?: boolean; variant?: 'outline' | 'text' }) {
  const [state, setState] = useState<{ text: string; label: string } | null>(null)
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setState({ text, label: '已复制' }) }
    catch { setState({ text, label: '复制失败，请选中文字复制' }) }
  }
  return <button type="button" disabled={disabled} onClick={() => void copy()}
    className={`cursor-pointer rounded-lg px-3 py-2 text-[13px] text-ink-2 disabled:cursor-default disabled:opacity-40 ${variant === 'outline' ? 'border border-line' : 'hover:text-ink-1'}`}>
    <InkRewrite text={state?.text === text ? state.label : label} />
  </button>
}
