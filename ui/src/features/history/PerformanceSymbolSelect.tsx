import { Fragment, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { Select } from '@/components/ui/Select'
import { OverflowText } from '@/components/ui/OverflowText'
import { amount, lossClass } from './costs'
import type { CostView } from './localAnalysis'

const columns = 'grid grid-cols-[minmax(0,1fr)_5ch_5ch_5ch_5ch] items-baseline gap-x-2 text-xs tabular-nums'

export function PerformanceSymbolSelect({ result, scope, symbols, symbol, busy, onChange }: {
  result: CostView | null; scope: string; symbols: string[]; symbol: string; busy: boolean; onChange: (value: string) => void
}) {
  const [previous, setPrevious] = useState({ scope, result })
  if (scope !== previous.scope || (result && previous.result !== result)) setPrevious({ scope, result })
  const view = result ?? (scope === previous.scope ? previous.result : null)
  const rows = new Map(view?.rows.map(row => [row.symbol, row]))
  return <Select ariaLabel="绩效图品种" searchable floatingTitle="品种列表" menuWidth={402} appearance="inline" busy={busy}
    className="min-h-7! w-36 gap-1 px-2 text-xs" value={symbol} selectedValue={<OverflowText text={symbol || '全部品种'} />} onChange={onChange}
    menuHeader={<div className={`${columns} ml-[34px] mr-[14px] border-b border-line py-1 text-ink-2`}>
      <span>品种</span><span className="col-span-2 text-right">买入滑点 BP</span><span className="col-span-2 text-right">卖出滑点 BP</span>
    </div>}
    options={[{ value: '', label: '全部品种' }, ...symbols.map(name => ({
      value: name, label: name, content: <span className={columns}>
        <span className="truncate" title={name}>{name}</span>
        {(['buy', 'sell'] as const).map(side => {
          const group = rows.get(name)?.[side]
          const bp = group?.lossBp
          return <Fragment key={side}>
            <span className={`text-right ${lossClass(bp)}`}>{bp != null && bp > 0 ? '+' : ''}{amount(bp ?? null)}</span>
            <span className="text-right text-ink-2">{view ? `${group?.count ?? 0} 笔` : '—'}</span>
          </Fragment>
        })}
      </span>,
    }))]} />
}

const buckets = ['< −10', '−10 至 −5', '−5 至 −1', '−1 至 1', '1 至 5', '5 至 10', '≥ 10']

export function SymbolSlippage({ result, symbol, side, onSelect }: {
  result: CostView | null; symbol: string; side: string; onSelect: (symbol: string, side: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const row = result?.rows.find(row => row.symbol === symbol)
  const detail = result?.detail?.symbol === symbol && result.detail.side === side ? result.detail.result : null
  const peak = detail ? Math.max(...detail.histogram) : 0
  return (
    <section aria-label={`${symbol}逐笔滑点分布`} className="w-fit max-w-full space-y-1 py-1 text-xs">
      <header className="flex items-center justify-between gap-4">
        <button type="button" className="min-h-6! text-ink-2 hover:text-ink-1" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {detail ? `${detail.summary.count} 笔成交 · ${amount(detail.summary.lossBp)} BP` : '逐笔滑点'} <ChevronRight size={12} className={`inline ${expanded ? 'rotate-90' : ''}`} />
        </button>
        <div role="group" aria-label="详情方向" className="flex gap-2">
          {[['', '全部'], ['buy', '买入'], ['sell', '卖出'], ...(row?.hasUnknownSide ? [['none', '未知']] : [])].map(([value, label]) =>
            <button key={value} type="button" aria-pressed={side === value} onClick={() => onSelect(symbol, value)} className={`min-h-6! border-b ${side === value ? 'border-current font-medium' : 'border-transparent text-ink-3'}`}>{label}</button>)}
        </div>
      </header>
      {expanded && detail && <>
        <ul className="grid grid-cols-7 gap-1 text-center text-[10px] tabular-nums">
          {buckets.map((label, i) => <li key={label} aria-label={`${label} BP，${detail.histogram[i]} 笔`}>
            <div className="flex h-7 flex-col items-center justify-end border-b border-line">
              {detail.histogram[i] > 0 && <><span className="leading-none">{detail.histogram[i]}</span><span className="w-1/2 bg-ink-3" style={{ height: `${16 * detail.histogram[i] / peak}px` }} /></>}
            </div>
            <span className="text-ink-2">{label.replace(' 至 ', '…').replaceAll(' ', '')}</span>
          </li>)}
        </ul>
        <p className="flex flex-wrap gap-x-3 text-[11px] text-ink-2"><span>成本 {amount(detail.summary.cost)}</span><span>参考价覆盖 {detail.summary.coverage == null ? `${detail.summary.covered}/${detail.summary.count} 笔` : `${amount(detail.summary.coverage * 100)}%（成交额）`}</span></p>
      </>}
    </section>
  )
}
