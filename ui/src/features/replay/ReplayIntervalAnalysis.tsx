import { useMemo, useState } from 'react'
import { Select } from '@/components/ui/Select'
import { ValueSlider } from '@/components/ui/ValueSlider'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useLocalAnalysis } from '@/features/history/useLocalAnalysis'
import { intervalInputs, intervalMetadata, type IntervalSlice, type IntervalStats } from './intervalData'
import { replayNumber, replayTime } from './presentation'
import type { ReplayRange } from './types'

export function ReplayIntervalAnalysis({ slices, symbol, range, visibleRange, cursor }: {
  slices: IntervalSlice[]; symbol: string; range: ReplayRange; visibleRange: ReplayRange; cursor: number
}) {
  const [selection, setSelection] = useState(range)
  const [open, setOpen] = useState(false)
  const [levels, setLevels] = useState(1)
  const [source, setSource] = useState<'market' | 'own'>('market')
  const dataset = useMemo(() => open ? intervalInputs(slices, symbol) : null, [slices, symbol, open])
  const metadata = useMemo(() => intervalMetadata(slices, symbol, selection), [slices, symbol, selection])
  const query = useMemo(() => ({ start: selection.start_us, end: selection.end_us, levels, tick: metadata.tick }), [selection, levels, metadata.tick])
  const local = useLocalAnalysis<typeof query, IntervalStats>('replay', dataset, query)
  const volume = local.value?.[source]
  const maximum = Math.max(1, ...volume?.profile.map(([, quantity]) => quantity) ?? [])
  const coverage = (value: number) => `${(value * 100).toFixed(1)}% 时间覆盖`
  return <section aria-label="回放区间统计" className="shrink-0 border-t border-line px-3 text-xs">
    <button type="button" aria-expanded={open} aria-controls="replay-interval-statistics" className="min-h-9 text-accent" onClick={() => setOpen(value => !value)}>{open ? '收起区间统计' : '展开区间统计'}</button>
    <div className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}><div className="min-h-0 overflow-hidden" inert={!open} aria-hidden={!open}><div id="replay-interval-statistics" className="max-h-64 overflow-auto pb-3">
    <div className="flex flex-wrap items-center gap-2"><h3 className="font-medium">区间统计</h3>
      <button type="button" className="min-h-9 rounded border border-line px-2" onClick={() => setSelection(visibleRange)}>分析当前视野</button>
      <Select ariaLabel="统计盘口档数" value={levels} onChange={setLevels} options={[1, 5, 10, 20].map(value => ({ value, label: `前 ${value} 档` }))} />
      <span className="text-ink-3">{replayTime(selection.start_us)} → {replayTime(selection.end_us)} · [起点, 终点)</span>
    </div>
    <div className="grid gap-x-6 sm:grid-cols-2">
      <ValueSlider label="统计起点" min={range.start_us} max={Math.max(range.start_us, selection.end_us - 1)} step={1} value={selection.start_us} formatValue={replayTime} onChange={start_us => setSelection(value => ({ ...value, start_us }))} />
      <ValueSlider label="统计终点" min={Math.min(range.end_us, selection.start_us + 1)} max={range.end_us} step={1} value={selection.end_us} formatValue={replayTime} onChange={end_us => setSelection(value => ({ ...value, end_us }))} />
    </div>
    <p className="mb-3 text-ink-3">按已加载记录统计{selection.end_us > cursor ? '，包含游标之后的记录' : ''}。缺口不延展盘口，未知挂量和不足档数不按零计算。</p>
    <ErrorNotice intent="read" title="区间统计不可用" error={local.error} variant="compact" />
    {local.pending && <p role="status" className="text-ink-3">正在计算区间统计</p>}
    {local.value && <>
      <dl className="mb-3 flex flex-wrap gap-x-8 gap-y-2">
        <div><dt className="text-ink-3">时间加权价差</dt><dd className="num">{replayNumber(local.value.spread)} · {coverage(local.value.spread_coverage)}</dd></div>
        <div><dt className="text-ink-3">前 {levels} 档挂量不平衡 (买−卖)/(买+卖)</dt><dd className="num">{replayNumber(local.value.imbalance)} · {coverage(local.value.imbalance_coverage)}</dd></div>
      </dl>
      <div className="mb-2 flex flex-wrap items-center gap-2"><Select ariaLabel="成交统计来源" value={source} onChange={setSource} options={[{ value: 'market', label: '市场成交' }, { value: 'own', label: '账户成交' }]} />
        <span className="text-ink-3">{source === 'market' ? `市场成交${metadata.availability === 'unavailable' ? '不可用' : metadata.availability === 'partial' ? '覆盖不完整' : '已录制'}${metadata.sources.length ? ` · ${metadata.sources.join('、')}` : ''}` : '仅账户成交，不代表市场总成交量'}</span>
      </div>
      {source === 'market' && metadata.availability === 'unavailable' ? <p className="text-ink-3">渠道没有录制市场成交流，无法计算市场 VWAP 和成交量分布。</p> : <>
        <p className="mb-2 num">VWAP {replayNumber(volume?.vwap)} · 成交量 {replayNumber(volume?.quantity)} · {volume?.count} 笔 · 排除身份不稳定、冲突或数值缺失 {volume?.omitted} 条记录</p>
        {!metadata.tick && <p className="text-ink-3">缺少一致的价格最小变动单位，成交量价格分布不可用。</p>}
        {!!volume?.profile.length && <div className="max-h-48 overflow-auto"><table className="w-full max-w-xl text-right"><caption className="mb-1 text-left text-ink-3">按价格分布 · 桶宽 {replayNumber(volume.bin_width)} · 最多 128 桶，与缩放像素无关</caption><thead className="sr-only"><tr><th>价格桶下沿</th><th>成交量</th><th>相对成交量</th></tr></thead><tbody>{volume.profile.map(([price, quantity]) => <tr key={price}><th className="py-1 pr-3 font-normal num">{replayNumber(price)}</th><td className="w-24 num">{replayNumber(quantity)}</td><td className="w-1/2 pl-3"><div className="h-2 bg-accent" aria-hidden style={{ width: `${quantity / maximum * 100}%` }} /></td></tr>)}</tbody></table></div>}
      </>}
    </>}
    </div></div></div>
  </section>
}
