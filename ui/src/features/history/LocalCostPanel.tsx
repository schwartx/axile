import { SectionLabel } from '@/components/ui/Card'
import { Select } from '@/components/ui/Select'
import { amount, coverageText, feeText } from './costs'
import type { LocalResult } from './localAnalysis'

export function LocalCostPanel({ result, symbols, symbol, side, onSymbol, onSide }: {
  result: LocalResult | null; symbols: string[]; symbol: string; side: string; onSymbol: (symbol: string) => void; onSide: (side: string) => void
}) {
  const summary = result?.summary
  const labels = ['< −10', '−10 至 −5', '−5 至 −1', '−1 至 1', '1 至 5', '5 至 10', '≥ 10']
  const maximum = Math.max(1, ...(result?.histogram ?? []))
  return <section aria-label="窗口成本分析" className="border-t border-line py-4 text-xs">
    <div className="mb-3 flex flex-wrap items-center gap-2"><SectionLabel>窗口成本分析</SectionLabel>
      <Select ariaLabel="成本品种" value={symbol} onChange={onSymbol} options={[{ value: '', label: '全部品种' }, ...symbols.map(value => ({ value, label: value }))]} />
      <Select ariaLabel="成本方向" value={side} onChange={onSide} options={[{ value: '', label: '全部方向' }, { value: 'buy', label: '买入' }, { value: 'sell', label: '卖出' }, { value: 'none', label: '方向未知' }]} />
      <span className="text-ink-3">完整加载窗口 · 跟随图表选区 · 本地计算</span>
    </div>
    {!summary ? <p role="status" className="text-ink-3">正在计算成本</p> : <>
      <dl className="mb-3 flex flex-wrap gap-x-6 gap-y-2">
        <div><dt className="text-ink-3">成交额</dt><dd className="num">{amount(summary.value)}{!summary.amountComplete && '（部分）'}</dd></div>
        <div><dt className="text-ink-3">滑点成本</dt><dd className="num">{amount(summary.cost)}</dd></div>
        <div><dt className="text-ink-3">加权滑点</dt><dd className="num">{amount(summary.lossBp)} BP</dd></div>
        <div><dt className="text-ink-3">有效参考价</dt><dd>{coverageText(summary)}</dd></div>
        <div><dt className="text-ink-3">实收费用（分币种）</dt><dd>{feeText(summary)} · {summary.feeCovered}/{summary.count} 笔</dd></div>
      </dl>
      <p className="mb-3 text-ink-3">{summary.count} 笔成交 · {summary.estimated ?? 0} 笔使用执行时间 · 正滑点表示成本；手续费保留原币种</p>
      <div className="grid gap-6 md:grid-cols-2">
        <div><p className="mb-2 text-ink-3">逐笔滑点分布（BP，不按成交额加权）</p>
          {labels.map((label, index) => <div key={label} className="grid grid-cols-[6rem_1fr_3rem] items-center gap-2 py-1"><span className="num">{label}</span><div className="h-2 rounded bg-fill" aria-hidden><div className="h-2 rounded bg-accent" style={{ width: `${(result!.histogram[index] ?? 0) / maximum * 100}%` }} /></div><span className="num text-right">{result!.histogram[index]}</span></div>)}
          <p className="mt-1 text-ink-3">参考价或方向未知：{result!.unknown} 笔</p>
        </div>
        <div className="max-h-64 overflow-auto"><table className="w-full text-right"><caption className="sr-only">按品种汇总成本</caption><thead className="text-ink-3"><tr><th className="text-left font-normal">品种</th><th className="font-normal">笔数</th><th className="font-normal">成本</th><th className="font-normal">滑点 BP</th></tr></thead><tbody>{result!.symbols.map(row => <tr key={row.symbol} className="border-t border-line"><th className="py-2 text-left font-normal">{row.symbol}</th><td className="num">{row.summary.count}</td><td className="num">{amount(row.summary.cost)}</td><td className="num">{amount(row.summary.lossBp)}</td></tr>)}</tbody></table></div>
      </div>
    </>}
  </section>
}
