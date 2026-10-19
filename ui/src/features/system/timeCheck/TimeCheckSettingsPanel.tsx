import { useId, useState } from 'react'
import { AccessibilitySwitch } from '@/components/ui/AccessibilitySwitch'
import { ParameterNumberInput } from '@/components/ui/ParameterNumberInput'
import { Select } from '@/components/ui/Select'
import { ScheduleFields } from '@/features/setup/ScheduleFields'
import type { TimerEditorState } from '@/features/setup/cron'
import type { TimeCheckSettings } from '@/lib/api/timeCheck'
import { MOTION_LAYOUT } from '@/lib/viewTransition'

const sourceOptions = [
  { value: 'aliyun' as const, label: '阿里云', description: 'ntp.aliyun.com' },
  { value: 'tencent' as const, label: '腾讯云', description: 'ntp.tencent.com' },
  { value: 'tuna' as const, label: '清华 TUNA', description: 'ntp.tuna.tsinghua.edu.cn' },
  { value: 'custom' as const, label: '自定义服务器' },
]

/** 横版弹窗右栏：行内编辑参数，自动检查启用后就地展开排程。 */
export function TimeCheckSettingsPanel({ draft, onChange, timer, onTimerChange, rawError, nextRun, conflict, onReload }: {
  draft: TimeCheckSettings
  onChange: (value: TimeCheckSettings) => void
  timer: TimerEditorState
  onTimerChange: (value: TimerEditorState) => void
  rawError: string | null
  nextRun: string
  conflict: boolean
  onReload: () => void
}) {
  const id = useId()
  const [threshold, setThreshold] = useState(String(draft.threshold_ms))
  const [port, setPort] = useState(String(draft.custom_port))
  const thresholdError = !Number.isInteger(draft.threshold_ms) || draft.threshold_ms < 1 || draft.threshold_ms > 86400000
  const portError = !Number.isInteger(draft.custom_port) || draft.custom_port < 1 || draft.custom_port > 65535
  return <div className="space-y-6">
    {conflict && <p role="alert" className="text-sm text-warn">
      设置已在其他窗口更新。
      <button type="button" className="ml-2 cursor-pointer underline" onClick={onReload}>载入最新设置并放弃本地修改</button>
    </p>}
    <section className="space-y-4">
      <div className="grid gap-2 sm:grid-cols-[100px_minmax(0,1fr)] sm:items-center">
        <p id={`${id}-source`} className="text-[14px] font-medium text-ink-2">参考时间源</p>
        <Select<TimeCheckSettings['source']> ariaLabel="参考时间源" value={draft.source} options={sourceOptions}
          onChange={source => {
            const custom_port = portError && source !== 'custom' ? 123 : draft.custom_port
            if (custom_port !== draft.custom_port) setPort(String(custom_port))
            onChange({ ...draft, source, custom_port })
          }} className="w-full justify-between px-3 py-2.5 text-[14px]" />
      </div>
      <div className={`grid transition-[grid-template-rows] ${MOTION_LAYOUT} ${draft.source === 'custom' ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`} inert={draft.source !== 'custom'}>
        <div className="min-h-0 overflow-hidden">
          <div className="space-y-4 pb-1">
            <label className="block space-y-2 text-[14px] text-ink-2">
              <span>服务器域名或 IP</span>
              <input value={draft.custom_host} onChange={event => onChange({ ...draft, custom_host: event.target.value })}
                autoCapitalize="none" spellCheck={false} placeholder="ntp.example.com"
                className="w-full rounded-[9px] border border-ink-3/30 bg-surface px-3 py-2.5 text-[14px] text-ink-1 outline-none transition-colors focus:border-accent" />
            </label>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <p className="pt-2 text-[14px] text-ink-2">UDP 端口</p>
              <ParameterNumberInput label="UDP 端口" value={port} step={1} min={1} max={65535}
                error={portError ? '请输入 1–65535 的整数' : undefined}
                onChange={value => { setPort(value); onChange({ ...draft, custom_port: value.trim() ? Number(value) : Number.NaN }) }} />
            </div>
          </div>
        </div>
      </div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1 pt-2">
          <p className="text-[14px] font-medium text-ink-2">允许偏差</p>
          <p className="text-xs text-ink-3">超过此值时显示告警</p>
        </div>
        <ParameterNumberInput label="允许偏差" value={threshold} unit="ms" step={50} min={1} max={86400000}
          error={thresholdError ? '请输入 1–86400000 的整数' : undefined}
          onChange={value => { setThreshold(value); onChange({ ...draft, threshold_ms: value.trim() ? Number(value) : Number.NaN }) }} />
      </div>
    </section>
    <section className="space-y-4 border-t border-line pt-5">
      <div className="flex items-center justify-between gap-4">
        <div className="space-y-1">
          <h3 id={`${id}-auto`} className="text-[14px] font-semibold">自动检查</h3>
          <p id={`${id}-auto-help`} className="text-xs text-ink-3">由服务端定时执行，关闭浏览器后仍有效</p>
        </div>
        <AccessibilitySwitch checked={draft.enabled} labelledBy={`${id}-auto`} describedBy={`${id}-auto-help`}
          onChange={enabled => onChange({ ...draft, enabled })} />
      </div>
      <div className={`grid transition-[grid-template-rows] ${MOTION_LAYOUT} ${draft.enabled ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`} inert={!draft.enabled}>
        <div className="min-h-0 overflow-hidden"><div className="space-y-4 pt-1">
          <ScheduleFields value={timer} patch={patch => onTimerChange({ ...timer, ...patch })} scheduleKind="continuous" rawErr={rawError} tabFade={false} />
          {rawError && <p className="text-sm text-warn">{rawError}</p>}
        </div></div>
      </div>
      <p className="text-xs text-ink-3">下次检查：{nextRun}</p>
    </section>
  </div>
}
