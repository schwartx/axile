import { useState, type ReactElement } from 'react'
import { Check, Copy } from 'lucide-react'
import { Tooltip } from '@/components/ui/Tooltip'

/** 列表名称的身份信息；只使用列表已有数据，不触发额外请求。 */
export function IdentityHover({ name, id, kind, createdAt, children }: {
  name: string
  id: number | null
  kind: '账户' | '组合'
  createdAt?: string
  children: ReactElement
}) {
  return <Tooltip delay={600} interactive content={
    <IdentityContent key={`${kind}-${id}`} name={name} id={id} kind={kind} createdAt={createdAt} />
  }>{children}</Tooltip>
}

function IdentityContent({ name, id, kind, createdAt }: {
  name: string
  id: number | null
  kind: string
  createdAt?: string
}) {
  const [copied, setCopied] = useState<'name' | 'id' | null>(null)
  const [failed, setFailed] = useState(false)
  const copy = async (field: 'name' | 'id', value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(field)
      setFailed(false)
    } catch {
      setCopied(null)
      setFailed(true)
    }
  }
  const copyButton = (field: 'name' | 'id', value: string) => <button type="button"
    aria-label={`复制${kind}${field === 'name' ? '名称' : ' ID'} ${value}`}
    className="ml-auto grid size-6 shrink-0 place-items-center rounded text-ink-2 hover:text-accent focus-visible:outline-2 focus-visible:outline-accent"
    onClick={() => void copy(field, value)}>
    {copied === field ? <Check size={13} aria-hidden /> : <Copy size={13} aria-hidden />}
  </button>
  return <div className="min-w-44 space-y-2 text-left font-normal">
    <div className="flex items-start gap-3">
      <span className="min-w-0 select-text break-words font-semibold text-ink-1">{name}</span>
      {copyButton('name', name)}
    </div>
    <div className="flex items-center gap-3 text-ink-3">
      <span>{kind} ID</span>
      <span className="select-text font-mono text-ink-2">{id ?? '—'}</span>
      {id != null && copyButton('id', String(id))}
    </div>
    {createdAt && <div className="text-xs text-ink-3">创建于 {createdAt}</div>}
    {failed && <div role="status" className="text-xs text-warn">
      复制失败，请选择文本手动复制
    </div>}
  </div>
}
