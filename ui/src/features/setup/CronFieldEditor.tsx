import { useId, useRef, useState } from 'react'
import { Check } from 'lucide-react'
import { ChoiceGroup } from '@/components/ui/ChoiceGroup'
import { StepperNumberInput } from '@/components/ui/StepperNumberInput'
import { cronFieldError, cronValueLabel, type CronFieldDefinition, type CronFieldDraft, type CronFieldMode } from '@/features/setup/cronSettingModel'
import { useRemountFade } from '@/lib/viewTransition'

/** 横排模式共用参数区；字段草稿由规则持有，切换字段或模式不丢失。 */
export function CronFieldEditor({ field, value, onChange }: {
  field: CronFieldDefinition
  value: CronFieldDraft
  onChange: (value: CronFieldDraft) => void
}) {
  const id = useId()
  const error = cronFieldError(field, value)
  const [modeChange, setModeChange] = useState(0)
  // 仅主动切换模式时淡入参数；回填另一条表达式或字段不播放入场。
  const modeFade = useRemountFade(modeChange)
  const options: { value: CronFieldMode; label: string }[] = field.id === 'week'
    ? [{ value: 'every', label: '每周' }, { value: 'specific', label: '指定值' }]
    : [{ value: 'every', label: field.everyLabel }, { value: 'interval', label: '按间隔' }, { value: 'range', label: '范围' }, { value: 'specific', label: '指定值' }]
  const patch = (next: Partial<CronFieldDraft>) => onChange({ ...value, ...next, original: undefined })
  const numberInput = (part: 'start' | 'intervalStart' | 'end' | 'step', label: string) => <StepperNumberInput
    value={value[part]} onChange={(next) => patch({ [part]: next })} size="sm" step={1} selectOnFocus
    displayValue={value[part].trim() && Number.isFinite(Number(value[part])) ? Number(value[part]) : undefined}
    min={part === 'step' ? 1 : field.min} max={part === 'step' ? Math.max(1, field.max - Number(value.intervalStart)) : field.max}
    ariaLabel={`${field.label}${label}`} invalid={!!error} describedBy={error ? `${id}-error` : undefined} />
  const intervalUnit = field.id === 'day' ? '天' : field.id === 'month' ? '月' : field.label
  const startUnit = field.id === 'hour' ? '点' : field.id === 'minute' ? '分钟' : field.unit

  return <div className="min-w-0 py-2 text-sm text-ink-2">
    <ChoiceGroup ariaLabel={`${field.label}设置方式`} value={value.mode} options={options}
      onChange={(mode) => {
        if (mode === value.mode) return
        setModeChange((current) => current + 1)
        patch({ mode })
      }} invalid={!!error} describedBy={error ? `${id}-error` : undefined}
      className="[&>button]:px-3 [&>button]:py-1.5 [&>button]:text-[13px]" />
    <div key={value.mode} className={`mt-4 ${modeFade ? 'panel-fade-in' : ''}`}>
      {value.mode === 'interval' ? <div className="flex flex-wrap items-center gap-x-2 gap-y-2 text-[13px]">
          <span>{field.id === 'minute' ? '从第' : '从'}</span>{numberInput('intervalStart', '间隔起始值')}<span>{startUnit}开始，每隔</span>
          {numberInput('step', '间隔')}<span>{intervalUnit}</span>
        </div>
        : value.mode === 'range' ? <div className="flex flex-wrap items-center gap-2 text-[13px]">
          <span>从</span>{numberInput('start', '范围起始值')}<span>到</span>{numberInput('end', '结束值')}<span>{startUnit}</span>
        </div>
        : value.mode === 'specific' ? <CronValuePicker key={field.id} field={field} values={value.values} onChange={(values) => patch({ values })} />
        : value.mode === 'raw' ? <p className="text-xs leading-relaxed text-ink-3">当前字段使用复杂表达式，可在底部原文编辑区修改，或选择一种设置方式替换。</p>
        : null}
    </div>
    {error && <p id={`${id}-error`} role="alert" className="mt-2 text-xs text-warn">{error}</p>}
  </div>
}

function CronValuePicker({ field, values, onChange }: {
  field: CronFieldDefinition
  values: number[]
  onChange: (values: number[]) => void
}) {
  const [focused, setFocused] = useState(values[0] ?? field.min)
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const items = Array.from({ length: field.max - field.min + 1 }, (_, index) => index + field.min)
  const gridClass = field.id === 'minute' ? 'grid-cols-6 lg:grid-cols-10'
    : field.id === 'week' ? 'grid-cols-4 lg:grid-cols-7'
      : field.id === 'month' ? 'grid-cols-3 lg:grid-cols-4' : 'grid-cols-6'

  return <div className="space-y-2">
    <div className="flex items-center justify-between text-xs text-ink-3">
      <span>已选 {values.length} 项</span>
      <button type="button" onClick={() => onChange([])} disabled={!values.length}
        className="cursor-pointer rounded px-1 py-1 text-accent hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:text-ink-3 disabled:opacity-50">清空</button>
    </div>
    <div role="group" aria-label={`选择${field.label}`} className={`grid gap-1.5 ${gridClass}`}
      onKeyDown={(event) => {
        const index = refs.current.indexOf(event.target as HTMLButtonElement)
        if (index < 0) return
        const columns = getComputedStyle(event.currentTarget).gridTemplateColumns.split(' ').length
        let next: number
        if (event.key === 'ArrowRight') next = index + 1
        else if (event.key === 'ArrowLeft') next = index - 1
        else if (event.key === 'ArrowDown') next = index + columns
        else if (event.key === 'ArrowUp') next = index - columns
        else if (event.key === 'Home') next = event.ctrlKey ? 0 : index - index % columns
        else if (event.key === 'End') next = event.ctrlKey ? items.length - 1 : index - index % columns + columns - 1
        else return
        event.preventDefault()
        refs.current[Math.max(0, Math.min(items.length - 1, next))]?.focus()
      }}>
      {items.map((item, index) => {
        const selected = values.includes(item)
        return <button key={item} ref={(node) => { refs.current[index] = node }} type="button" role="checkbox"
          aria-label={cronValueLabel(field, item)} aria-checked={selected} tabIndex={item === focused ? 0 : -1}
          onFocus={() => setFocused(item)}
          onClick={() => onChange(selected ? values.filter((entry) => entry !== item) : [...values, item].sort((a, b) => a - b))}
          className={`relative flex h-8 min-w-0 cursor-pointer items-center justify-center rounded-md border text-[13px] tabular-nums transition-colors duration-150 motion-reduce:transition-none focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${selected ? 'border-accent bg-accent-soft text-ink-1' : 'border-transparent bg-fill text-ink-2 hover:border-ink-3/40 hover:text-ink-1'}`}>
          <span>{field.id === 'week' || field.id === 'month' ? cronValueLabel(field, item) : item}</span>
          <Check size={9} aria-hidden="true" className={`absolute bottom-0.5 right-0.5 text-accent transition-opacity duration-150 motion-reduce:transition-none ${selected ? 'opacity-100' : 'opacity-0'}`} />
        </button>
      })}
    </div>
  </div>
}
