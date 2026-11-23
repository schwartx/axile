import { Fragment, useState } from 'react'
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

export function SymbolSlippage({ result, symbol, side, onSelect }: {
  result: CostView | null; symbol: string; side: string; onSelect: (symbol: string, side: string) => void
}) {
  const row = result?.rows.find(row => row.symbol === symbol)
  const summary = result?.detail?.symbol === symbol && result.detail.side === side ? result.detail.result.summary : null
  return (
    <section aria-label={`${symbol}成交滑点摘要`} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs tabular-nums">
      <div role="group" aria-label="成交方向" className="flex shrink-0 gap-2">
        {[['', '全部'], ['buy', '买入'], ['sell', '卖出'], ...(row?.hasUnknownSide ? [['none', '未知']] : [])].map(([value, label]) =>
          <button key={value} type="button" aria-pressed={side === value} onClick={() => onSelect(symbol, value)} className={`min-h-6! border-b ${side === value ? 'border-current font-medium' : 'border-transparent text-ink-3'}`}>{label}</button>)}
      </div>
      <span className="whitespace-nowrap text-ink-2">{summary ? `${summary.count} 笔成交 · ${amount(summary.lossBp)} BP` : '成交滑点 —'}</span>
      {summary && <p className="flex flex-wrap gap-x-2 text-[11px] text-ink-2">
        <span className="whitespace-nowrap">成本 {amount(summary.cost)}</span>
        <span className="whitespace-nowrap">参考价覆盖 {summary.coverage == null ? `${summary.covered}/${summary.count} 笔` : `${amount(summary.coverage * 100)}%（成交额）`}</span>
      </p>}
    </section>
  )
}
