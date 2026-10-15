import { useEffect, useId, useRef, useState } from 'react'
import { Pencil } from 'lucide-react'
import { Segmented } from '@/components/ui/Segmented'
import { StepperNumberInput } from '@/components/ui/StepperNumberInput'
import { quotaWarningCount, quotaWarningPercentError } from '@/features/account/quotaWarning'

/** 账户每日额度的行内预警编辑，合法修改进入页面草稿，Esc 恢复进入编辑前的值。 */
export function ControlWarningField({ limit, value, operationName, onChange, onInvalidChange }: {
  limit: number | null | undefined
  value: number | null
  operationName: string
  onChange: (value: number | null) => void
  onInvalidChange: (invalid: boolean) => void
}) {
  const id = useId()
  const input = useRef<HTMLInputElement>(null)
  const summary = useRef<HTMLButtonElement>(null)
  const editor = useRef<HTMLDivElement>(null)
  const previousValue = useRef<number | null>(null)
  const [editing, setEditing] = useState(false)
  const [enabled, setEnabled] = useState(false)
  const [draft, setDraft] = useState('80')
  const available = limit != null && limit > 0
  const activeEditing = editing && available
  const parsed = Number(draft)
  const draftError = draft.trim() === '' ? '请输入 1–99 的整数' : quotaWarningPercentError(parsed)
  const error = enabled ? draftError : null
  const invalid = activeEditing && Boolean(error)
  const count = available && !draftError ? quotaWarningCount(limit, parsed) : null

  useEffect(() => { onInvalidChange(invalid) }, [invalid, onInvalidChange])
  useEffect(() => () => onInvalidChange(false), [onInvalidChange])
  useEffect(() => { if (!available) setEditing(false) }, [available])
  useEffect(() => {
    if (!activeEditing) return
    if (enabled) input.current?.focus()
    else editor.current?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus()
  }, [activeEditing, enabled])

  const begin = () => {
    previousValue.current = value
    setDraft(String(value ?? 80))
    setEnabled(value != null)
    setEditing(true)
  }
  const setMode = (mode: 'off' | 'on') => {
    const next = mode === 'on'
    setEnabled(next)
    if (!next) onChange(null)
    else if (!draftError) onChange(parsed)
  }
  const setPercent = (text: string) => {
    setDraft(text)
    const percent = Number(text)
    if (enabled && text.trim() && !quotaWarningPercentError(percent)) onChange(percent)
  }
  const finish = (refocus = false) => {
    if (error) return
    setEditing(false)
    if (refocus) requestAnimationFrame(() => summary.current?.focus())
  }
  const cancel = () => {
    onChange(previousValue.current)
    setEditing(false)
    requestAnimationFrame(() => summary.current?.focus())
  }
  const unavailableText = limit === 0 ? '每日额度为 0，预警暂不生效' : '每日额度不限，预警暂不生效'
  const summaryText = value == null ? '未启用' : `${value}% · 达到 ${quotaWarningCount(limit ?? 0, value)} 次时提醒`
  const reveal = activeEditing && enabled

  return <div className="border-t border-line/70" aria-label={`${operationName}额度预警`}>
    <div className="group -mx-2 flex min-h-10 items-start gap-x-2.5 rounded-[8px] px-2 py-1.5 hover:bg-fill">
      <span className="w-24 flex-none pt-1.5 text-[13px] text-ink-2">额度预警</span>
      <div className="min-w-0 flex-1">
        <div className="grid min-h-7">
          <div inert={activeEditing} aria-hidden={activeEditing} className={`col-start-1 row-start-1 flex min-w-0 flex-wrap items-center gap-x-2.5 transition-opacity duration-200 motion-reduce:transition-none ${activeEditing ? 'pointer-events-none opacity-0' : 'opacity-100'}`}>
            <button ref={summary} type="button" disabled={!available} onClick={begin} aria-label={`调整${operationName}额度预警`} className="flex min-w-0 cursor-pointer items-center gap-2 text-left text-[14px] text-ink-1 hover:text-accent disabled:cursor-default disabled:text-ink-3">
              <span className="num">{available ? summaryText : unavailableText}</span>
              {available && <Pencil size={12} className="shrink-0 text-ink-3" aria-hidden />}
            </button>
            {available && value != null && <span className="flex items-baseline gap-2 text-xs"><span className="text-accent">已改</span><button type="button" aria-label={`恢复${operationName}额度预警默认值`} className="cursor-pointer text-accent" onClick={() => onChange(null)}>恢复</button></span>}
          </div>
          <div ref={editor} inert={!activeEditing} aria-hidden={!activeEditing} onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) finish()
          }} onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); cancel() } }} className={`col-start-1 row-start-1 min-w-0 transition-opacity duration-200 motion-reduce:transition-none ${activeEditing ? 'opacity-100' : 'pointer-events-none opacity-0'}`}>
            <div role="group" aria-label={`${operationName}额度预警开关`}><Segmented size="sm" value={enabled ? 'on' : 'off'} options={[{ value: 'off', label: '关闭' }, { value: 'on', label: '开启' }]} onChange={setMode} /></div>
            <div inert={!reveal} aria-hidden={!reveal} className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${reveal ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
              <div className="min-h-0 overflow-hidden">
                <div className="flex flex-wrap items-center gap-2 pt-2">
                  <label htmlFor={id} className="text-[13px] text-ink-2">用量达到</label>
                  <StepperNumberInput ref={input} id={id} ariaLabel={`${operationName}预警比例`} describedBy={`${id}-hint`} value={draft} onChange={setPercent} size="sm" step={1} min={1} max={99} unit="%" invalid={Boolean(error)} displayValue={!draftError ? parsed : undefined} onEnter={() => finish(true)} />
                  <span className="text-[13px] text-ink-2">时提醒</span>
                </div>
                <p id={`${id}-hint`} aria-live="polite" className={`pt-1.5 text-xs ${error ? 'text-warn' : 'text-ink-3'}`}>
                  {error ?? (count != null && limit != null ? count < limit ? `达到 ${count} 次时发飞书，剩余 ${limit - count} 次。` : `达到 ${count} 次、额度用尽时提醒。` : '')}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
}
