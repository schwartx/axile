import { useState } from 'react'
import { CircleHelp, Settings } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { Segmented } from '@/components/ui/Segmented'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Tooltip } from '@/components/ui/Tooltip'
import { closePolicySummary, closePriorityLabel } from '@/features/account/futuresClosePolicy'
import type { ClosePriority, FuturesClosePolicy, FuturesCloseCapability } from '@/types/api'

const priorities: { value: ClosePriority; label: string }[] = [
  { value: 'yesterday_first', label: '先昨后今' },
  { value: 'today_first', label: '先今后昨' },
]

function FeeDetails({ fee, shared = false }: { fee: string | null | undefined; shared?: boolean }) {
  if (!fee) return <span className="text-ink-3">—</span>
  return <div className={shared ? 'grid grid-cols-2 gap-x-5 gap-y-2' : 'space-y-2'}>{fee.split('；').map((entry, index) => {
    const match = entry.match(/^([^（）]+)（([^（）]+)）$/)
    return <div key={index} className="min-w-0 space-y-0.5">
      <p className="font-medium text-ink-2">{match ? match[1] : entry}</p>
      {match && <p className="text-xs leading-snug text-ink-3">{match[2]}</p>}
    </div>
  })}</div>
}

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
    <CenteredModal open={open} title="平仓规则" onClose={() => setOpen(false)} widthClassName="w-[1040px]" scrollBody={false}
      footer={<div className="flex items-center gap-3 border-t border-line px-[22px] py-2">
        <div className="mr-auto flex min-w-0 items-center gap-1.5 text-xs text-ink-3">
          <span>预设 {capability.preset_version}</span>
          <Tooltip content={<>空白表示未知，实际费用以账户费率为准。{capability.notice}</>}>
            <button type="button" aria-label="预设手续费来源" className="shrink-0 cursor-help text-ink-3 hover:text-ink-2"><CircleHelp size={14} aria-hidden /></button>
          </Tooltip>
        </div>
        <button type="button" className="cursor-pointer text-sm text-ink-2" onClick={() => setOpen(false)}>取消</button>
        <button type="button" className="shrink-0 cursor-pointer rounded-lg bg-accent px-3 py-1.5 text-sm text-white" onClick={() => { onChange(draft); setOpen(false) }}>完成</button>
      </div>}>
      <div className="flex min-h-0 flex-1 flex-col gap-3">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
          {capability.modes.length > 1 ? <Segmented size="sm" value={draft.mode} options={capability.modes.map((mode) => ({ value: mode, label: mode === 'fee' ? '低手续费优先' : '按品种规则' }))} onChange={(mode) => setDraft((d) => ({ ...d, mode }))} /> : <p className="text-sm text-ink-1">按品种规则</p>}
          <div className="flex items-center gap-2">
            <span className="text-xs text-ink-3">默认顺序</span>
            <Segmented size="sm" value={draft.fallback} options={priorities} onChange={(fallback) => setDraft((d) => ({ ...d, fallback }))} />
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2 text-xs text-ink-3">
          <p aria-live="polite"><InkRewrite text={draft.mode === 'fee' ? '低手续费优先；同费或不可比较时：人工覆盖 → 预设 → 默认顺序' : '人工覆盖 → 程序预设 → 默认顺序'} tone="label" /></p>
          <Tooltip content="常规调仓与清仓共用。优先仓位不足时继续平另一类仓位；不支持选择平今平昨的交易所按交易所规则执行。">
            <button type="button" aria-label="平仓规则说明" className="shrink-0 cursor-help text-ink-3 hover:text-ink-2"><CircleHelp size={14} aria-hidden /></button>
          </Tooltip>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2">
          <input aria-label="搜索或添加品种" placeholder="搜索品种或交易所，例如 AG" value={search} onChange={(e) => { setSearch(e.target.value); setError('') }} className="min-w-0 flex-1 basis-[180px] rounded-lg border border-line bg-transparent px-3 py-1.5 text-sm" />
          <button type="button" className="shrink-0 cursor-pointer text-[13px] text-accent" onClick={add}>添加品种</button>
          <button type="button" className="shrink-0 cursor-pointer text-[13px] text-accent disabled:cursor-default disabled:text-ink-3" disabled={!Object.keys(draft.overrides).length} onClick={() => setDraft((d) => ({ ...d, overrides: {} }))}>恢复全部预设</button>
        </div>
        {error && <p role="alert" className="shrink-0 text-xs text-warn">{error}</p>}
        <div className="isolate min-h-0 max-h-[440px] flex-1 overflow-auto rounded-lg border border-line">
          <table className="w-full min-w-[620px] text-left text-[13px]">
            <thead className="sticky top-0 z-20 whitespace-nowrap bg-surface"><tr className="border-b border-line text-ink-3"><th className="p-3 font-normal">品种</th><th className="p-3 font-normal">平昨手续费</th><th className="p-3 font-normal">平今手续费</th><th className="p-3 font-normal">平仓顺序</th><th className="p-3 font-normal">来源</th></tr></thead>
            <tbody className="relative z-0">{shown.map((product) => {
              const preset = presets.get(product)
              const modified = Object.hasOwn(draft.overrides, product)
              const priority = draft.overrides[product] ?? preset?.priority ?? draft.fallback
              const sameFee = Boolean(preset?.yesterday_fee) && preset?.yesterday_fee === preset?.today_fee
              return <tr key={product} className="border-b border-line last:border-0">
                <td className="p-3"><span className="font-medium text-ink-1">{product}</span><span className="ml-2 text-[11px] text-ink-3">{preset?.exchange}</span></td>
                {sameFee ? <td colSpan={2} className="p-3">
                  <p className="mb-1 text-[11px] text-ink-3">平今、平昨相同</p>
                  <FeeDetails fee={preset?.yesterday_fee} shared />
                </td> : <>
                  <td className="p-3 align-top"><FeeDetails fee={preset?.yesterday_fee} /></td>
                  <td className="p-3 align-top"><FeeDetails fee={preset?.today_fee} /></td>
                </>}
                <td className="p-3">{preset?.can_choose === false ? <span className="text-ink-3">按交易所规则执行</span> : <div role="group" aria-label={`${product} 平仓顺序`} className="whitespace-nowrap"><Segmented size="sm" value={priority} options={priorities} onChange={(next) => setPriority(product, next)} /></div>}</td>
                <td className="p-3 whitespace-nowrap">{modified ? <><span className="text-accent">已修改</span><button type="button" aria-label={`${product} 恢复预设`} className="ml-2 cursor-pointer text-ink-3 hover:text-accent" onClick={() => restore(product)}>恢复预设</button></> : <span className="text-ink-3">{preset ? '预设' : `默认${closePriorityLabel(priority)}`}</span>}</td>
              </tr>
            })}</tbody>
          </table>
          {!shown.length && <p className="p-4 text-sm text-ink-3">没有匹配的品种，可输入字母代码添加。</p>}
        </div>
      </div>
    </CenteredModal>
  </>
}
