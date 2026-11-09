import { useEffect, useId, useState, type ReactNode } from 'react'
import { MacTimePicker } from '@/components/ui/MacTimePicker'
import { AlgorithmEditor } from '@/features/setup/AlgorithmEditor'
import { describeAlgorithmRef } from '@/features/setup/algorithms'
import {
  algorithmScheduleErrors, algorithmParameterSummary, algorithmTimeLabel, type AlgorithmRuleDraft,
} from '@/features/account/algorithmSchedule'
import type { AlgorithmRef, TradeChannel } from '@/types/api'

const ACTION = 'shrink-0 rounded-chip px-2 py-1 text-sm text-ink-2 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent'

export function EditableAlgorithm({ label, channel, value, onChange, onValidationError, nameVtName,
  children, action, initiallyExpanded = false }: {
  label: string
  channel: TradeChannel
  value: AlgorithmRef
  onChange: (value: AlgorithmRef) => void
  onValidationError?: (error: string | null) => void
  nameVtName?: string
  children?: ReactNode
  action?: ReactNode
  initiallyExpanded?: boolean
}) {
  const [expanded, setExpanded] = useState(initiallyExpanded)
  const panelId = useId()
  return <div className="min-w-0 py-3">
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <div className="text-[15px] font-medium">{label}</div>
        <p className="mt-1 break-words text-sm text-ink-2">
          <span style={nameVtName ? { viewTransitionName: nameVtName } : undefined}>{describeAlgorithmRef(value)}</span>
          {algorithmParameterSummary(value) && ` · ${algorithmParameterSummary(value)}`}
        </p>
      </div>
      <button type="button" className={ACTION} aria-expanded={expanded} aria-controls={panelId}
        aria-label={`${expanded ? '收起' : '修改'}${label}`} onClick={() => setExpanded(!expanded)}>
        {expanded ? '收起' : '修改'}
      </button>
      {action}
    </div>
    <div id={panelId} inert={!expanded} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden">
        <div className="space-y-4 pt-4">
          {children}
          <AlgorithmEditor slot="trade" channel={channel} value={value} onChange={next => { if (next) onChange(next) }}
            onValidationError={onValidationError} />
        </div>
      </div>
    </div>
  </div>
}

export function AlgorithmScheduleEditor({ channel, defaultAlgorithm, value, onChange, onValidationError }: {
  channel: TradeChannel
  defaultAlgorithm: AlgorithmRef
  value: AlgorithmRuleDraft[]
  onChange: (value: AlgorithmRuleDraft[]) => void
  onValidationError: (error: string | null) => void
}) {
  const [editorErrors, setEditorErrors] = useState<Record<string, string | null>>({})
  const errors = algorithmScheduleErrors(value)
  const error = errors.find(Boolean) ?? value.map(rule => editorErrors[rule.key]).find(Boolean) ?? null
  useEffect(() => onValidationError(error), [error, onValidationError])
  const update = (key: string, patch: Partial<AlgorithmRuleDraft>) => onChange(value.map(rule => rule.key === key ? { ...rule, ...patch } : rule))
  return <div className="mt-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="text-sm font-medium text-ink-2">指定时段 · 北京时间</span>
      <button type="button" className={ACTION} onClick={() => onChange([...value, {
        key: crypto.randomUUID(), start: '', end: '', algorithm: structuredClone(defaultAlgorithm),
      }])}>＋ 添加时段</button>
    </div>
    <div className="divide-y divide-line">
      {value.map((rule, index) => <div key={rule.key}>
        <EditableAlgorithm label={algorithmTimeLabel(rule.start, rule.end)} channel={channel} value={rule.algorithm}
          onChange={algorithm => update(rule.key, { algorithm })} initiallyExpanded={!rule.start || !rule.end}
          onValidationError={next => setEditorErrors(previous => previous[rule.key] === next ? previous : { ...previous, [rule.key]: next })}
          action={<button type="button" className={`${ACTION} hover:text-warn`} aria-label={`删除${algorithmTimeLabel(rule.start, rule.end)}`}
            onClick={() => onChange(value.filter(item => item.key !== rule.key))}>删除</button>}>
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm text-ink-2">从</span>
            <MacTimePicker compact value={rule.start} onChange={start => update(rule.key, { start })} ariaLabel={`第 ${index + 1} 个时段开始时间`} />
            <span className="text-sm text-ink-2">至{rule.end && rule.end < rule.start ? '次日' : ''}</span>
            <MacTimePicker compact value={rule.end} onChange={end => update(rule.key, { end })} ariaLabel={`第 ${index + 1} 个时段结束时间`} />
          </div>
        </EditableAlgorithm>
        {(errors[index] || editorErrors[rule.key]) && <p role="alert" className="pb-3 text-sm text-warn">{errors[index] || editorErrors[rule.key]}</p>}
      </div>)}
    </div>
    <p className="mt-2 text-xs leading-relaxed text-ink-3">
      {value.length > 0 ? '其余时间使用平时的算法。' : ''}按调仓开始时间选择，执行期间保持不变。
    </p>
  </div>
}
