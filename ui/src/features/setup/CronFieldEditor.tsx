import { useId } from 'react'
import { StepperNumberInput } from '@/components/ui/StepperNumberInput'
import { cronFieldError, cronValueLabel, type CronFieldDefinition, type CronFieldDraft, type CronFieldMode } from '@/features/setup/cronSettingModel'

/** 对齐参考组件：纵向单选项，选中后就地展开数字输入或复选框。 */
export function CronFieldEditor({ field, value, onChange }: {
  field: CronFieldDefinition
  value: CronFieldDraft
  onChange: (value: CronFieldDraft) => void
}) {
  const id = useId()
  const error = cronFieldError(field, value)
  const values = Array.from({ length: field.max - field.min + 1 }, (_, index) => index + field.min)
  const options: { value: CronFieldMode; label: string }[] = field.id === 'week'
    ? [{ value: 'every', label: '每周' }, { value: 'specific', label: '指定' }]
    : [{ value: 'every', label: field.everyLabel }, { value: 'interval', label: '每隔' }, { value: 'range', label: '周期' }, { value: 'specific', label: '指定' }]
  const patch = (next: Partial<CronFieldDraft>) => onChange({ ...value, ...next, original: undefined })
  const numberInput = (part: 'start' | 'end' | 'step', label: string) => <StepperNumberInput
    value={value[part]} onChange={(next) => patch({ [part]: next })} size="sm" step={1}
    min={part === 'step' ? 1 : field.min} max={part === 'step' ? Math.max(1, field.max - Number(value.start)) : field.max}
    ariaLabel={`${field.label}${label}`} invalid={!!error} describedBy={error ? `${id}-error` : undefined} />
  const intervalUnit = field.id === 'day' ? '天' : field.id === 'month' ? '月' : field.label
  const startUnit = field.id === 'hour' ? '点' : field.unit
  // 分钟选项吸收编辑区剩余高度，将留白分配到行间；矮屏仍保留六行的最小高度。
  const stretchMinutes = field.id === 'minute' && value.mode === 'specific'

  return <fieldset className={`min-w-0 py-2 text-sm text-ink-2 ${stretchMinutes ? 'flex flex-col gap-2 lg:flex-1' : 'space-y-2'}`} aria-describedby={error ? `${id}-error` : undefined}>
    <legend className="sr-only">{field.label}设置方式</legend>
    {value.mode === 'raw' && <p className="text-xs leading-relaxed text-ink-3">当前字段使用复杂表达式，可在底部原文编辑区修改，或选择一种设置方式替换。</p>}
    {options.map((option) => <div key={option.value} className={stretchMinutes && option.value === 'specific' ? 'flex flex-1 flex-col' : 'shrink-0'}>
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2">
        <label className="inline-flex shrink-0 cursor-pointer items-center gap-2">
          <input type="radio" name={id} value={option.value} checked={value.mode === option.value}
            onChange={() => patch({ mode: option.value })} className="h-4 w-4 accent-accent" />
          {option.label}
        </label>
        {option.value === 'interval' && value.mode === 'interval' && <div className="flex flex-wrap items-center gap-2">
          {numberInput('step', '间隔')}<span>{intervalUnit}（从</span>{numberInput('start', '起始值')}<span>{startUnit}开始）</span>
        </div>}
        {option.value === 'range' && value.mode === 'range' && <div className="flex flex-wrap items-center gap-2">
          <span>从</span>{numberInput('start', '起始值')}<span>到</span>{numberInput('end', '结束值')}<span>{startUnit}</span>
        </div>}
      </div>
      {option.value === 'specific' && value.mode === 'specific' && <div role="group" aria-label={`选择${field.label}`}
        className={`ml-[22px] mt-2.5 grid gap-x-1 ${field.id === 'month' || field.id === 'week' ? 'grid-cols-3' : field.id === 'minute' ? 'auto-rows-[22px] grid-cols-6 gap-y-2 lg:flex-1 lg:content-between lg:grid-cols-10 lg:gap-y-0' : 'grid-cols-6'}`}>
        {values.map((item) => <label key={item} className="inline-flex min-w-0 cursor-pointer items-center gap-1.5 text-sm leading-[22px]">
          <input type="checkbox" checked={value.values.includes(item)} aria-label={cronValueLabel(field, item)} className="h-4 w-4 shrink-0 accent-accent"
            onChange={(event) => patch({ values: event.target.checked ? [...value.values, item].sort((a, b) => a - b) : value.values.filter((entry) => entry !== item) })} />
          <span>{field.id === 'month' || field.id === 'week' ? cronValueLabel(field, item) : item}</span>
        </label>)}
      </div>}
    </div>)}
    {error && <p id={`${id}-error`} role="alert" className="text-xs text-warn">{error}</p>}
  </fieldset>
}
