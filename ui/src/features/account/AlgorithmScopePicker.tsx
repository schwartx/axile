import { useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { Select } from '@/components/ui/Select'
import type { ChannelCapability } from '@/types/api'

/** 渠道提供输入格式；后端负责规范化及品种存在性校验。 */
export function AlgorithmScopePicker({ capability, products, value, onChange, onAdd, onDelete }: {
  capability: ChannelCapability['algorithm_scope']; products: string[]; value: string
  onChange: (value: string) => void; onAdd: (value: string) => void; onDelete: (value: string) => void
}) {
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  if (!capability && products.length === 0) return null
  const add = () => {
    const key = code.trim()
    if (!capability || !new RegExp(`^(?:${capability.pattern})$`, 'i').test(key)) {
      setError(`请输入渠道支持的品种代码，如 ${capability?.example ?? ''}`)
      return
    }
    if (products.some(product => product.toLowerCase() === key.toLowerCase())) {
      setError('该品种已配置，请在左侧选择')
      return
    }
    onAdd(key); setCode(''); setError(null)
  }
  return <div className="space-y-1.5 border-t border-line py-2">
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-sm text-ink-2">配置范围</span>
      <Select ariaLabel="算法配置范围" value={value} onChange={onChange} searchable
        options={[{ value: '', label: '账户通用' }, ...products.map(product => ({ value: product, label: product }))]} />
      <button type="button" disabled={!value} onClick={() => onDelete(value)}
        className="inline-flex items-center gap-1 rounded-chip px-2 py-1.5 text-xs text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-45 disabled:cursor-not-allowed">
        <Trash2 size={14} aria-hidden />删除品种配置
      </button>
      {capability && <div className="flex min-w-0 flex-wrap items-center gap-2">
        <input aria-label="新增算法品种代码" aria-describedby="algorithm-scope-help" aria-invalid={!!error} maxLength={80}
          placeholder={capability.example} value={code} onChange={event => { setCode(event.target.value); setError(null) }}
          onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); add() } }}
          className="w-36 rounded-chip border border-line bg-surface px-2.5 py-1.5 text-sm text-ink-1 placeholder:text-ink-2 focus-visible:outline-2 focus-visible:outline-accent" />
        <button type="button" onClick={add} className="inline-flex items-center gap-1 rounded-chip border border-line px-2.5 py-1.5 text-xs text-accent hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent">
          <Plus size={14} aria-hidden />添加品种
        </button>
      </div>}
    </div>
    <p id="algorithm-scope-help" className={error ? 'text-xs text-warn' : 'text-xs text-ink-2'} role={error ? 'alert' : undefined}>
      {error ?? (value ? `${value} 使用自己的默认算法和时段；时段外回到品种默认。` : `未单独配置的品种沿用账户通用设置。${capability?.description ?? ''}`)}
    </p>
  </div>
}
