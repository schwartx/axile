import { useState } from 'react'
import { Settings } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { Segmented } from '@/components/ui/Segmented'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { closePolicySummary, closePriorityLabel } from '@/features/account/futuresClosePolicy'
import type { ClosePriority, FuturesClosePolicy, FuturesCloseCapability } from '@/types/api'

const priorities: { value: ClosePriority; label: string }[] = [
  { value: 'yesterday_first', label: '先昨后今' },
  { value: 'today_first', label: '先今后昨' },
]

/** 账户级平仓规则；完成只写页面草稿，页面保存后才用于后续执行。 */
export function FuturesClosePolicyEditor({ value, capability, onChange }: {
  value: FuturesClosePolicy
  capability: FuturesCloseCapability
  onChange: (value: FuturesClosePolicy) => void
}) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(value)
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const begin = () => { setDraft(structuredClone(value)); setSearch(''); setError(''); setOpen(true) }
  const setPriority = (product: string, priority: ClosePriority) => setDraft((d) => ({ ...d, overrides: { ...d.overrides, [product]: priority } }))
  const restore = (product: string) => setDraft((d) => {
    const overrides = { ...d.overrides }
    delete overrides[product]
    return { ...d, overrides }
  })
  const presets = new Map(capability.presets.map((p) => [p.product, p]))
  const products = [...new Set([...presets.keys(), ...Object.keys(draft.overrides)])].sort()
  const shown = products.filter((p) => `${p} ${presets.get(p)?.exchange ?? ''}`.toUpperCase().includes(search.trim().toUpperCase()))
  const add = () => {
    const product = search.trim().toUpperCase()
    if (!/^[A-Z]+$/.test(product)) { setError('请输入字母品种代码，例如 AG，不填写月份'); return }
    setPriority(product, presets.get(product)?.priority ?? draft.fallback)
    setError('')
  }
  return <>
    <button type="button" onClick={begin} className="inline-flex cursor-pointer items-center gap-2 text-left text-[14px] text-ink-2 hover:text-accent" aria-label="设置平仓规则">
      <InkRewrite text={closePolicySummary(value)} tone="label" />
      <Settings size={16} aria-hidden />
    </button>
    <CenteredModal open={open} title="平仓规则" subtitle="用于常规调仓和清仓。完成后还需在执行算法页面保存。" onClose={() => setOpen(false)} widthClassName="w-[800px]"
      footer={<div className="flex justify-end gap-3 border-t border-line px-[22px] py-3">
        <button type="button" className="cursor-pointer text-sm text-ink-2" onClick={() => setOpen(false)}>取消</button>
        <button type="button" className="cursor-pointer rounded-lg bg-accent px-4 py-2 text-sm text-white" onClick={() => { onChange(draft); setOpen(false) }}>完成</button>
      </div>}>
      <div className="space-y-5">
        {capability.modes.length > 1 ? <Segmented value={draft.mode} options={capability.modes.map((mode) => ({ value: mode, label: mode === 'fee' ? '低手续费优先' : '按品种规则' }))} onChange={(mode) => setDraft((d) => ({ ...d, mode }))} /> : <p className="text-[15px] text-ink-1">按品种规则</p>}
        <p className="text-[13px] leading-6 text-ink-3" aria-live="polite">{draft.mode === 'fee'
          ? '优先平预计手续费较低的仓位；费用相同或费率不可用时，依次使用人工覆盖、预设和默认顺序。'
          : '优先使用人工修改，其次使用程序预设；未覆盖的品种使用默认顺序。优先仓位不足时继续平另一类仓位。'}</p>
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm text-ink-2">默认顺序</span>
          <Segmented size="sm" value={draft.fallback} options={priorities} onChange={(fallback) => setDraft((d) => ({ ...d, fallback }))} />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[14px] text-ink-1">{draft.mode === 'fee' ? '兜底品种规则' : '品种规则'}</span>
          <button type="button" className="cursor-pointer text-[13px] text-accent disabled:cursor-default disabled:text-ink-3" disabled={!Object.keys(draft.overrides).length} onClick={() => setDraft((d) => ({ ...d, overrides: {} }))}>恢复全部预设</button>
        </div>
        <div className="flex gap-2">
          <input aria-label="搜索或添加品种" placeholder="搜索品种或交易所，例如 AG" value={search} onChange={(e) => { setSearch(e.target.value); setError('') }} className="min-w-0 flex-1 rounded-lg border border-line bg-transparent px-3 py-2 text-sm" />
          <button type="button" className="cursor-pointer text-sm text-accent" onClick={add}>添加品种</button>
        </div>
        {error && <p role="alert" className="text-sm text-warn">{error}</p>}
        <div className="max-h-[360px] overflow-auto rounded-lg border border-line">
          <table className="w-full min-w-[620px] text-left text-[13px]">
            <thead className="sticky top-0 whitespace-nowrap bg-surface"><tr className="border-b border-line text-ink-3"><th className="p-3 font-normal">品种</th><th className="p-3 font-normal">平昨手续费</th><th className="p-3 font-normal">平今手续费</th><th className="p-3 font-normal">平仓顺序</th><th className="p-3 font-normal">来源</th></tr></thead>
            <tbody>{shown.map((product) => {
              const preset = presets.get(product)
              const modified = Object.hasOwn(draft.overrides, product)
              const priority = draft.overrides[product] ?? preset?.priority ?? draft.fallback
              return <tr key={product} className="border-b border-line last:border-0">
                <td className="p-3"><span className="font-medium text-ink-1">{product}</span><span className="ml-2 text-[11px] text-ink-3">{preset?.exchange}</span></td>
                <td className="p-3 text-ink-2">{preset?.yesterday_fee ?? '—'}</td>
                <td className="p-3 text-ink-2">{preset?.today_fee ?? '—'}</td>
                <td className="p-3">{preset?.can_choose === false ? <span className="text-ink-3">按交易所规则执行</span> : <select aria-label={`${product} 平仓顺序`} value={priority} onChange={(e) => setPriority(product, e.target.value as ClosePriority)} className="rounded border border-line bg-transparent px-2 py-1 text-ink-2">{priorities.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}</select>}</td>
                <td className="p-3 whitespace-nowrap">{modified ? <><span className="text-accent">已修改</span><button type="button" aria-label={`${product} 恢复预设`} className="ml-2 cursor-pointer text-ink-3 hover:text-accent" onClick={() => restore(product)}>恢复预设</button></> : <span className="text-ink-3">{preset ? '预设' : `默认${closePriorityLabel(priority)}`}</span>}</td>
              </tr>
            })}</tbody>
          </table>
          {!shown.length && <p className="p-4 text-sm text-ink-3">没有匹配的品种，可输入字母代码添加。</p>}
        </div>
        <p className="text-[12px] leading-5 text-ink-3">预设 {capability.preset_version}。手续费说明暂留空，等待补充；空白不代表零费用。预设说明不代替账户实际费率。{capability.notice}</p>
      </div>
    </CenteredModal>
  </>
}
