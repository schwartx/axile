import { useState } from 'react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import type { ErrorInfo } from '@/lib/errorInfo'

/** 可嵌入现有弹窗；正文和复制均使用冻结的同一份详情。 */
export function ErrorDetailsContent({ info }: { info: ErrorInfo }) {
  return <div className="min-w-0 text-[13px]">
    <dl className="mb-4 space-y-2">{info.evidence.map(({ label, value }, index) => <div key={index}>
      <dt className="text-ink-3">{label}</dt><dd className="mt-1 whitespace-pre-wrap [overflow-wrap:anywhere] font-mono">{value}</dd>
    </div>)}</dl>
    {info.details.map(({ label, value }, index) => <div key={index} className="mb-4 rounded-xl border border-line bg-fill p-3">
      <div className="mb-2 text-[12px] text-ink-3">{label}</div>
      <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[13px] leading-relaxed text-ink-1">{value}</pre>
    </div>)}
  </div>
}

export function ErrorCopyButton({ text, label = '复制错误信息', disabled = false }: { text: string; label?: string; disabled?: boolean }) {
  const [state, setState] = useState<{ text: string; label: string } | null>(null)
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setState({ text, label: '已复制' }) }
    catch { setState({ text, label: '复制失败，请选中文字复制' }) }
  }
  return <button type="button" disabled={disabled} onClick={() => void copy()}
    className="cursor-pointer rounded-lg border border-line px-3 py-2 text-[13px] text-ink-2 disabled:cursor-default disabled:opacity-40">
    <InkRewrite text={state?.text === text ? state.label : label} />
  </button>
}
