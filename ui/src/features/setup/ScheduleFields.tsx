/** 通用排程字段：账户与系统诊断共享快捷、高级、自定义编辑。 */
import type { ReactNode } from 'react'
import { Check } from 'lucide-react'
import { Segmented } from '@/components/ui/Segmented'
import { TimerAdvanced, TimerCustom } from '@/features/setup/TimerAdvanced'
import { DEFAULT_PRESET, PRESETS, cronToExpr, resolveCronList, ruleFromPreset, type ScheduleKind, type NightSchedule, type TimerEditorState } from '@/features/setup/cron'

export function ScheduleFields({ value: v, patch, scheduleKind, nightSchedule, nightControls, rawErr, tabFade = false }: {
  value: TimerEditorState
  patch: (value: Partial<TimerEditorState>) => void
  scheduleKind: ScheduleKind
  nightSchedule?: NightSchedule | null
  nightControls?: ReactNode
  rawErr: string | null
  tabFade?: boolean
}) {
  const setTab = (tab: 'quick' | 'advanced' | 'custom') => {
    if (tab === 'custom') {
      const rawCron = v.rawCron.trim()
        ? v.rawCron
        : cronToExpr(resolveCronList(scheduleKind, { ...v, supN: 0 }, nightSchedule))
      patch({ timerTab: 'custom', rawCron })
      return
    }
    if (tab === 'advanced') {
      if (v.scheduleRules.length >= 1) {
        patch({
          timerTab: 'advanced',
          selectedRuleId: v.selectedRuleId || v.scheduleRules[0]!.id,
        })
        return
      }
      const rule = ruleFromPreset(scheduleKind, v.presetIds[0] ?? DEFAULT_PRESET[scheduleKind])
      patch({ timerTab: 'advanced', scheduleRules: [rule], selectedRuleId: rule.id })
      return
    }
    patch({ timerTab: 'quick' })
  }

  const togglePreset = (id: string) => {
    if (v.scheduleRules.length >= 2) {
      patch({ presetIds: [id] })
      return
    }
    const rule = ruleFromPreset(scheduleKind, id)
    patch({ presetIds: [id], scheduleRules: [rule], selectedRuleId: rule.id })
  }

  const presetCardT =
    'transition-[border-color,background-color,box-shadow] duration-200 ease-[cubic-bezier(0.4,0,0.2,1)] motion-reduce:transition-none'
  return <>
      <div>
        <Segmented<'quick' | 'advanced' | 'custom'>
          size="sm"
          value={v.timerTab}
          onChange={setTab}
          options={[
            { value: 'quick', label: '快捷' },
            { value: 'advanced', label: '高级' },
            { value: 'custom', label: '自定义' },
          ]}
        />
      </div>

      <div key={tabFade ? v.timerTab : undefined} className={tabFade ? 'panel-fade-in' : undefined}>
        {v.timerTab === 'quick' ? (
          <div>
            <div className="mb-2">
              <label className="text-sm font-[640]">节奏</label>
            </div>
            <div className="flex flex-wrap gap-2">
              {PRESETS[scheduleKind].map((p) => {
                const on = v.presetIds.includes(p.id)
                return (
                  <button
                    key={p.id}
                    type="button"
                    aria-pressed={on}
                    onClick={() => togglePreset(p.id)}
                    className={`min-w-[92px] rounded-[11px] border p-2.5 text-left ${presetCardT} ${
                      on
                        ? 'border-accent bg-accent-soft shadow-[inset_0_0_0_1px_var(--color-accent)]'
                        : 'border-line bg-surface'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 text-sm font-[640]">
                      <span>{p.label}</span>
                      <Check size={13} aria-hidden="true" className={`shrink-0 ${on ? '' : 'invisible'}`} />
                    </div>
                    {p.sub && <div className="text-xs text-ink-3">{p.sub}</div>}
                  </button>
                )
              })}
            </div>
            {nightControls}
          </div>
        ) : v.timerTab === 'advanced' ? (
          <TimerAdvanced
            market={scheduleKind}
            rules={v.scheduleRules}
            selectedId={v.selectedRuleId}
            onChangeRules={(scheduleRules, selectedRuleId) => patch({ scheduleRules, selectedRuleId })}
          />
        ) : (
          <TimerCustom
            rawCron={v.rawCron}
            rawErr={rawErr}
            onRawCron={(rawCron) => patch({ rawCron })}
          />
        )}
      </div>

  </>
}
