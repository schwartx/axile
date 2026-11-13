import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Trash2 } from 'lucide-react'
import { EdgeScrollHint } from '@/components/ui/EdgeScrollHint'
import { MacTimePicker } from '@/components/ui/MacTimePicker'
import { OverflowText } from '@/components/ui/OverflowText'
import { NumberTicker } from '@/components/ui/NumberTicker'
import { Select } from '@/components/ui/Select'
import { AlgorithmEditor } from '@/features/setup/AlgorithmEditor'
import { describeAlgorithmRef } from '@/features/setup/algorithms'
import { algorithmScheduleErrors, algorithmStartChange, algorithmTimeLabel, type AlgorithmRuleDraft } from '@/features/account/algorithmSchedule'
import { useScrollEdges } from '@/hooks/useScrollEdges'
import { withViewTransition } from '@/lib/viewTransition'
import type { AlgorithmRef, AlgorithmSlot, TradeChannel } from '@/types/api'

interface Props {
  channel: TradeChannel
  trade: AlgorithmRef
  empty: AlgorithmRef | null
  schedule: AlgorithmRuleDraft[]
  onTradeChange: (value: AlgorithmRef) => void
  onEmptyChange: (value: AlgorithmRef | null) => void
  onScheduleChange: (value: AlgorithmRuleDraft[]) => void
  errors: Record<string, string | null>
  onValidationError: (key: string, error: string | null) => void
  initialSlot?: AlgorithmSlot
  tradeNameVt?: string
  emptyNameVt?: string
  footer: ReactNode
  saveBar: ReactNode
  showEmpty?: boolean
  scopeLabel?: string
}

function AlgorithmPanel({ active, label, children }: { active: boolean; label: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const edges = useScrollEdges(ref)
  return <div className={`${active ? 'block' : 'hidden'} relative h-full min-h-0 max-w-[744px]`} inert={!active}>
    <div ref={ref} aria-label={label} className="quiet-scrollbar h-full min-h-0 overflow-y-auto overscroll-contain [scrollbar-gutter:stable] pr-2">
      <div>{children}</div>
    </div>
    <EdgeScrollHint edge="up" visible={edges.up} target={ref} label="参数" className="from-bg via-bg/80" />
    <EdgeScrollHint edge="down" visible={edges.down} target={ref} label="参数" className="from-bg via-bg/80" />
  </div>
}

const TIME_FORMAT = { minimumIntegerDigits: 2, maximumFractionDigits: 0, useGrouping: false } as const

function NextDayMarker({ active }: { active: boolean }) {
  return <span aria-hidden={!active} inert={!active}
    className={`inline-grid shrink-0 transition-[grid-template-columns] duration-200 ease-[cubic-bezier(.4,0,.2,1)] motion-reduce:transition-none ${active ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]'}`}>
    <span className="min-w-0 overflow-hidden whitespace-nowrap">次日</span>
  </span>
}

function TimeRangeLabel({ start, end }: { start: string; end: string }) {
  if (!start || !end) return <>新时段</>
  const time = (value: string) => <span className="inline-flex items-baseline whitespace-nowrap">
    <NumberTicker value={Number(value.slice(0, 2))} format={TIME_FORMAT} />
    :
    <NumberTicker value={Number(value.slice(3))} format={TIME_FORMAT} />
  </span>
  return <span aria-hidden className="num inline-flex flex-wrap items-baseline">
    {time(start)}<span className="mx-1 inline-flex items-baseline whitespace-nowrap">至<NextDayMarker active={end < start} /></span>{time(end)}
  </span>
}

function AlgorithmItem({ itemKey, label, time, algorithm, selected, error, onSelect }: {
  itemKey: string; label: string; time?: { start: string; end: string }; algorithm: AlgorithmRef | null; selected: boolean; error?: string | null; onSelect: () => void
}) {
  return <button type="button" aria-label={`编辑${label}`} aria-pressed={selected} onClick={onSelect}
    style={{ '--algorithm-item-name': `algorithm-item-${itemKey}` } as CSSProperties}
    className={`algorithm-config-item relative w-full min-w-0 cursor-pointer px-2.5 py-2 text-left transition-colors duration-150 motion-reduce:transition-none focus-visible:outline-2 focus-visible:outline-accent ${selected ? 'text-ink-1' : 'text-ink-2 hover:text-ink-1'}`}>
    <span aria-hidden className={`absolute bottom-3 left-0 top-3 w-[2px] bg-accent ${selected ? 'algorithm-config-selection opacity-100' : 'opacity-0'}`} />
    <span className="flex items-center gap-2 text-sm font-medium">
      <span className="min-w-0 flex-1">{time ? <TimeRangeLabel start={time.start} end={time.end} /> : label}</span>
      {error && <span className="shrink-0 text-xs text-warn">有误</span>}
    </span>
    <OverflowText className="mt-0.5 text-xs text-ink-2" text={algorithm ? describeAlgorithmRef(algorithm) : '使用默认清仓逻辑'} />
  </button>
}

/** 左栏选择配置，右栏保留各自表单实例与滚动位置，切换不丢未完成的参数草稿。 */
export function AlgorithmWorkbench({ channel, trade, empty, schedule, onTradeChange, onEmptyChange,
  onScheduleChange, errors, onValidationError, initialSlot = 'trade', tradeNameVt, emptyNameVt, footer, saveBar,
  showEmpty = true, scopeLabel = '账户通用' }: Props) {
  const [selection, setSelection] = useState<string>(initialSlot)
  const listRef = useRef<HTMLDivElement>(null)
  const active = selection === 'trade' || selection === 'empty' || schedule.some(rule => rule.key === selection) ? selection : 'trade'
  useLayoutEffect(() => {
    const list = listRef.current
    if (list && list.clientHeight > 0) list.querySelector('[aria-pressed="true"]')?.scrollIntoView({ block: 'nearest', behavior: 'instant' })
  }, [active])
  const timeErrors = algorithmScheduleErrors(schedule)
  const select = (key: string) => { if (key !== active) withViewTransition(() => setSelection(key), 'algorithm-config') }
  const add = () => {
    const rule = { key: crypto.randomUUID(), start: '', end: '', algorithm: structuredClone(trade) }
    withViewTransition(() => { onScheduleChange([...schedule, rule]); setSelection(rule.key) }, 'algorithm-config')
  }
  const update = (key: string, patch: Partial<AlgorithmRuleDraft>) => onScheduleChange(schedule.map(rule => rule.key === key ? { ...rule, ...patch } : rule))
  const item = (key: string, label: string, algorithm: AlgorithmRef | null, error = errors[key]) =>
    <AlgorithmItem key={key} itemKey={key} label={label} algorithm={algorithm} selected={active === key} error={error} onSelect={() => select(key)} />

  return <div aria-label="算法配置工作区" className="grid h-full min-h-0 min-w-0 grid-cols-1 grid-rows-[auto_minmax(0,1fr)_auto_auto] gap-x-5 min-[900px]:grid-cols-[clamp(240px,26%,300px)_minmax(0,1fr)] min-[900px]:grid-rows-[auto_auto_minmax(0,1fr)_auto]">
    <div className="col-start-1 row-start-1 flex items-center gap-2 pb-3 min-[900px]:hidden">
      <Select ariaLabel="选择算法配置" value={active} onChange={select} className="min-w-0 flex-1"
        options={[{ value: 'trade', label: '默认算法' }, ...schedule.map(rule => ({ value: rule.key, label: algorithmTimeLabel(rule.start, rule.end) })), ...(showEmpty ? [{ value: 'empty', label: '清仓算法' }] : [])]} />
      <button type="button" onClick={add} className="shrink-0 rounded-chip border border-line px-3 py-2 text-sm text-accent">＋ 添加时段</button>
    </div>

    <div className="col-start-1 row-start-1 hidden items-center justify-between gap-2 pb-2 min-[900px]:flex">
      <h2 className="text-sm font-semibold">{scopeLabel} · 调仓算法</h2>
      <button type="button" onClick={add} className="cursor-pointer rounded-chip border border-line px-2.5 py-1.5 text-xs text-accent hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent">＋ 添加时段</button>
    </div>
    <div className="col-start-1 row-start-2 hidden pb-2 min-[900px]:block">
      {item('trade', '默认算法', trade)}
    </div>
    <div ref={listRef} aria-label="调仓时段" className="quiet-scrollbar col-start-1 row-start-3 hidden min-h-0 overflow-y-auto overscroll-contain [scrollbar-gutter:stable] min-[900px]:block">
      {schedule.map((rule, index) => <AlgorithmItem key={rule.key} itemKey={rule.key} time={rule}
        label={rule.start && rule.end ? algorithmTimeLabel(rule.start, rule.end) : '新时段'} algorithm={rule.algorithm}
        selected={active === rule.key} error={timeErrors[index] || errors[rule.key]} onSelect={() => select(rule.key)} />)}
      {schedule.length === 0 && <p className="px-3 py-4 text-xs leading-relaxed text-ink-3">全天使用默认算法。添加时段后，可在指定时间使用另一套参数。</p>}
    </div>
    <div className="col-start-1 row-start-3 min-w-0 border-t border-line pt-2 min-[900px]:row-start-4">
      {showEmpty && <div className="hidden pb-2 min-[900px]:block">{item('empty', '清仓算法', empty)}</div>}
      {footer}
    </div>

    <section aria-label="当前算法编辑区" className="contents min-[900px]:col-start-2 min-[900px]:row-span-4 min-[900px]:row-start-1 min-[900px]:grid min-[900px]:min-h-0 min-[900px]:min-w-0 min-[900px]:grid-rows-[minmax(0,1fr)_auto] min-[900px]:border-l min-[900px]:border-line min-[900px]:pl-5">
      <div className="algorithm-config-editor col-start-1 row-start-2 min-h-0 min-w-0 overflow-hidden bg-bg min-[900px]:row-start-1">
        <AlgorithmPanel active={active === 'trade'} label="默认算法参数">
          <h2 className="mb-1 text-base font-semibold">默认算法</h2>
          <p className="mb-4 text-xs text-ink-3">{scopeLabel}未覆盖的时间使用这套配置。</p>
          <AlgorithmEditor slot="trade" channel={channel} value={trade} onChange={value => { if (value) onTradeChange(value) }}
            onValidationError={error => onValidationError('trade', error)} nameVtName={tradeNameVt} />
          {errors.trade && <p role="alert" className="py-2 text-sm text-warn">{errors.trade}</p>}
        </AlgorithmPanel>
        {schedule.map((rule, index) => <AlgorithmPanel key={rule.key} active={active === rule.key} label={`时段参数 ${index + 1}`}>
          <div className="mb-2 flex flex-wrap items-center gap-3">
            <span className="text-sm text-ink-2">从</span>
            <MacTimePicker value={rule.start} onChange={start => update(rule.key, algorithmStartChange(rule, start))} ariaLabel="开始时间" />
            <span className="inline-flex items-baseline whitespace-nowrap text-sm text-ink-2">至<NextDayMarker active={!!rule.end && rule.end < rule.start} /></span>
            <MacTimePicker value={rule.end} onChange={end => update(rule.key, { end, autoEnd: false })} ariaLabel="结束时间" />
            <span className="text-xs text-ink-3">北京时间</span>
            <button type="button" className="ml-auto inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-chip border border-line bg-fill/40 px-2.5 py-1.5 text-xs text-ink-1 transition-colors duration-150 hover:border-warn/50 hover:bg-warn/5 hover:text-warn active:bg-warn/10 focus-visible:outline-2 focus-visible:outline-accent motion-reduce:transition-none"
              onClick={() => withViewTransition(() => { onScheduleChange(schedule.filter(item => item.key !== rule.key)); setSelection('trade') }, 'algorithm-config')}><Trash2 size={14} aria-hidden />删除时段</button>
          </div>
          <p className="mb-4 text-xs text-ink-3">按调仓开始时间选择，执行期间保持不变。</p>
          {timeErrors[index] && <p role="alert" className="mb-3 text-sm text-warn">{timeErrors[index]}</p>}
          <AlgorithmEditor slot="trade" channel={channel} value={rule.algorithm} onChange={value => { if (value) update(rule.key, { algorithm: value }) }}
            onValidationError={error => onValidationError(rule.key, error)} />
          {errors[rule.key] && <p role="alert" className="py-2 text-sm text-warn">{errors[rule.key]}</p>}
        </AlgorithmPanel>)}
        {showEmpty && <AlgorithmPanel active={active === 'empty'} label="清仓算法参数">
          <h2 className="mb-4 text-base font-semibold">清仓算法</h2>
          <AlgorithmEditor slot="empty" channel={channel} value={empty} allowClear onChange={onEmptyChange}
            onValidationError={error => onValidationError('empty', error)} nameVtName={emptyNameVt} />
          {errors.empty && <p role="alert" className="py-2 text-sm text-warn">{errors.empty}</p>}
        </AlgorithmPanel>}
      </div>
      <div className="col-start-1 row-start-4 min-w-0 min-[900px]:row-start-2">{saveBar}</div>
    </section>
  </div>
}
