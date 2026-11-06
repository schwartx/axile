import { useId, useRef, useState } from 'react'
import { Clock3 } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { OverflowText } from '@/components/ui/OverflowText'
import { Select } from '@/components/ui/Select'
import { Segmented } from '@/components/ui/Segmented'
import { CronFieldEditor } from '@/features/setup/CronFieldEditor'
import { CronSettingHelp } from '@/features/setup/CronSettingHelp'
import { cronToExpr } from '@/features/setup/cron'
import { CRON_SETTING_PRESETS } from '@/features/setup/cronSettingPresets'
import {
  CRON_FIELDS, DEFAULT_CRON_RULE, cronRuleError, describeCronRule, parseCronRule, parseCronSetting, updateCronRuleField,
  type CronFieldDraft, type CronRuleDraft,
} from '@/features/setup/cronSettingModel'

/** 对齐参考的左右布局与编辑流程；确认后才将弹框草稿写回自定义输入框。 */
export function CronSetting({ value, onConfirm }: { value: string; onConfirm: (value: string) => void }) {
  const [open, setOpen] = useState(false)
  const [rules, setRules] = useState<CronRuleDraft[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [fieldIndex, setFieldIndex] = useState(0)
  const [showHelp, setShowHelp] = useState(false)
  const tabsId = useId()
  const listRef = useRef<HTMLDivElement>(null)
  const editorRef = useRef<HTMLDivElement>(null)
  const selectedIndex = Math.max(0, rules.findIndex((rule) => rule.id === selectedId))
  const selected = rules[selectedIndex]
  const field = CRON_FIELDS[fieldIndex]!
  const selectedField = selected?.fields?.[fieldIndex]
  const errors = rules.map(cronRuleError)
  const descriptions = rules.map(describeCronRule)
  const expression = cronToExpr(rules.map((rule) => rule.raw))

  const begin = () => {
    const next = parseCronSetting(value)
    setRules(next)
    setSelectedId(next[0]!.id)
    setFieldIndex(0)
    setShowHelp(false)
    setOpen(true)
  }
  const replaceRule = (next: CronRuleDraft) => setRules((current) => current.map((rule) => rule.id === next.id ? next : rule))
  const changeField = (draft: CronFieldDraft) => {
    if (selected) replaceRule(updateCronRuleField(selected, fieldIndex, draft))
  }
  const addRule = () => {
    const next = parseCronRule(DEFAULT_CRON_RULE)
    setRules((current) => [...current, next])
    setSelectedId(next.id)
    // 保持当前字段标签，连续添加后仍可直接操作同一个时间单位。
    window.requestAnimationFrame(() => listRef.current?.lastElementChild?.scrollIntoView({ block: 'nearest' }))
  }
  const removeRule = (id: string) => {
    if (rules.length === 1) return
    const next = rules.filter((rule) => rule.id !== id)
    setRules(next)
    if (selected?.id === id) setSelectedId(next[Math.min(selectedIndex, next.length - 1)]!.id)
  }
  const selectPreset = (preset: string | null) => {
    if (!preset || !selected) return
    if (preset.includes('|')) {
      const next = parseCronSetting(preset)
      setRules(next)
      setSelectedId(next[0]!.id)
    } else {
      replaceRule(parseCronRule(preset, selected.id))
    }
  }
  const confirm = () => {
    if (!rules.length || errors.some(Boolean)) return
    onConfirm(expression)
    setOpen(false)
  }

  return <>
    <button type="button" onClick={begin} aria-haspopup="dialog"
      className="inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-xs font-semibold text-accent transition-colors hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-accent">
      <Clock3 size={14} aria-hidden="true" />设置定时
    </button>
    {rules.length > 0 && <CenteredModal open={open} title="定时任务设置"
      titleSuffix={<span className="ml-3 inline-block rounded-md bg-fill px-2 py-0.5 align-middle text-xs font-normal text-ink-2">北京时间</span>}
      onClose={() => setOpen(false)} widthClassName="w-[1080px] h-[min(760px,86dvh)]" scrollBody={false}
      footer={<div className="mx-[22px] flex justify-end gap-2 border-t border-line py-3">
        <button type="button" onClick={() => setOpen(false)} className="rounded-[9px] border border-line px-4 py-2 text-sm text-ink-2 hover:bg-fill">取消</button>
        <button type="button" onClick={confirm} disabled={!rules.length || errors.some(Boolean)}
          className="cursor-pointer rounded-[9px] bg-ink-1 px-4 py-2 text-sm font-semibold text-surface disabled:cursor-default disabled:opacity-45">确定</button>
      </div>}>
      {/* 窄屏只滚动正文；双栏时各区域独立滚动，标题和操作始终可见。 */}
      {selected && <div className="quiet-scrollbar grid min-h-0 flex-1 auto-rows-max grid-cols-1 gap-4 overflow-y-auto overscroll-contain py-1 pr-1 lg:auto-rows-auto lg:grid-cols-[400px_minmax(0,1fr)] lg:grid-rows-1 lg:overflow-hidden lg:pr-0">
        <section className="flex min-h-0 min-w-0 flex-col rounded-lg border border-line p-3" aria-label="定时表达式列表">
          <div className="mb-2 flex shrink-0 items-center justify-between gap-3">
            <span className="text-sm font-semibold">定时表达式列表<span className="ml-2 rounded-md bg-fill px-1.5 py-0.5 text-xs font-normal tabular-nums text-ink-3">{rules.length}</span></span>
            <button type="button" onClick={addRule} className="shrink-0 cursor-pointer rounded-md bg-ink-1 px-2.5 py-1.5 text-xs font-semibold text-surface">添加表达式</button>
          </div>
          <div ref={listRef} className="quiet-scrollbar space-y-2 lg:min-h-0 lg:flex-1 lg:overflow-y-auto lg:overscroll-contain lg:pr-1 lg:[scrollbar-gutter:stable]">
            {rules.map((rule, index) => <div key={rule.id}
              className={`flex items-start gap-2 rounded-lg border px-3 py-2 transition-colors ${errors[index] ? 'border-warn' : selected.id === rule.id ? 'border-accent bg-accent-soft' : 'border-line hover:border-accent/60'}`}>
              <button type="button" aria-label={`编辑表达式 ${index + 1}`} aria-pressed={selected.id === rule.id} onClick={() => setSelectedId(rule.id)} className="flex min-w-0 flex-1 items-start gap-2 text-left">
                <span aria-hidden="true" className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[11px] tabular-nums ${selected.id === rule.id ? 'bg-accent text-white' : 'bg-fill text-ink-3'}`}>{index + 1}</span>
                <span className="min-w-0 flex-1">
                  <span className={`line-clamp-3 text-[13px] leading-[22px] ${errors[index] ? 'text-warn' : 'text-ink-1'}`}>{descriptions[index]}</span>
                  <OverflowText text={rule.raw || '空表达式'} className="mt-1 font-mono text-xs leading-relaxed text-ink-3" />
                  {errors[index] && <span id={`${tabsId}-error-${rule.id}`} role="alert" className="mt-1 block text-xs leading-relaxed text-warn">{errors[index]}</span>}
                </span>
              </button>
              {rules.length > 1 && <button type="button" aria-label={`删除表达式 ${index + 1}`} onClick={() => removeRule(rule.id)} className="mt-0.5 shrink-0 px-1 py-0.5 text-xs text-ink-3 hover:text-bad hover:underline">删除</button>}
            </div>)}
          </div>
        </section>

        <div className="flex min-w-0 flex-col gap-2 lg:min-h-0">
          <div className="flex shrink-0 items-center justify-between gap-2">
            <span className="font-semibold text-accent">正在编辑第 {selectedIndex + 1} 个表达式：</span>
            <button type="button" onClick={() => { setShowHelp(!showHelp); editorRef.current?.scrollTo({ top: 0 }) }} aria-expanded={showHelp} className="shrink-0 text-xs text-accent hover:underline">{showHelp ? '隐藏帮助' : '查看帮助'}</button>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className="shrink-0 text-sm font-semibold">常用表达式</span>
            <Select<string | null> searchable value={null} options={CRON_SETTING_PRESETS} onChange={selectPreset} placeholder="选择常用表达式"
              ariaLabel="选择常用表达式" className="min-w-0 flex-1 justify-between px-3 py-1.5 text-sm" />
          </div>
          <div ref={editorRef} className="quiet-scrollbar lg:flex lg:min-h-0 lg:flex-1 lg:flex-col lg:overflow-y-auto lg:overscroll-contain lg:pr-1 lg:[scrollbar-gutter:stable]">
            {showHelp && <CronSettingHelp />}
            <div className="sticky top-0 z-10 shrink-0 bg-surface py-1">
              <Segmented value={field.id} options={CRON_FIELDS.map((item) => ({ value: item.id, label: item.label }))}
                onChange={(next) => setFieldIndex(CRON_FIELDS.findIndex((item) => item.id === next))}
                size="sm" ariaLabel="时间字段" panelId={`${tabsId}-panel`} className="w-full [&>button]:flex-1" />
            </div>
            <div role="tabpanel" id={`${tabsId}-panel`} aria-labelledby={`${tabsId}-panel-${field.id}`}>
              {selectedField ? <CronFieldEditor field={field} value={selectedField} onChange={changeField} />
                : <p className="py-4 text-sm leading-relaxed text-ink-3">请先在下方将当前表达式调整为五段，或选择常用表达式。</p>}
            </div>
          </div>
          <div className="h-[88px] shrink-0 border-t border-line pt-2">
            <label className="flex h-full flex-col gap-1.5">
              <span className="text-xs font-semibold text-ink-2">当前表达式</span>
              <textarea aria-label="当前表达式" spellCheck={false} value={selected.raw}
                aria-invalid={!!errors[selectedIndex] || undefined}
                aria-describedby={errors[selectedIndex] ? `${tabsId}-error-${selected.id}` : undefined}
                onChange={(event) => replaceRule(parseCronRule(event.target.value, selected.id))}
                className={`quiet-scrollbar min-h-0 w-full flex-1 resize-none rounded-[9px] border bg-surface px-3 py-2 font-mono text-xs leading-relaxed outline-none ${errors[selectedIndex] ? 'border-warn' : 'border-ink-3/30 focus:border-accent'}`} />
            </label>
          </div>
        </div>
      </div>}
    </CenteredModal>}
  </>
}
