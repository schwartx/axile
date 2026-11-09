import { useId } from 'react'
import { AccessibilitySwitch } from '@/components/ui/AccessibilitySwitch'
import { MacTimePicker } from '@/components/ui/MacTimePicker'
import { defaultGMSessions, type GMSessions } from '@/features/setup/cron'

const PHASES = [
  ['opening_cancelable', '早盘集合竞价 · 可撤单阶段', '09:15'],
  ['opening_locked', '早盘集合竞价 · 不可撤单阶段', '09:20'],
  ['closing_auction', '收盘集合竞价', '14:57'],
  ['after_hours', '盘后定价交易', '15:05'],
] as const

/** 阶段触发独立于盘中节奏；盘后能力尚未验证。 */
export function GMSessionControls({ channel, value, onChange }: {
  channel?: string | null
  value?: GMSessions | null
  onChange: (value: GMSessions) => void
}) {
  const id = useId()
  if (channel !== 'gm') return null
  const sessions = value ?? defaultGMSessions()
  return <div className="@container my-4 space-y-3">
    <p className="text-sm font-[640]">特殊交易阶段 · 仅股票（GM）</p>
    <div className="grid grid-cols-1 gap-x-6 gap-y-4 @min-[640px]:grid-cols-2">
      {PHASES.map(([key, label, time]) => {
        const unavailable = key === 'after_hours'
        const selected = sessions[key]
        return <div key={key} className="flex flex-wrap items-center gap-3">
          <AccessibilitySwitch checked={selected.enabled} disabled={unavailable} labelledBy={`${id}-${key}`}
            describedBy={`${id}-${key}-range`} onChange={(enabled) => onChange({ ...sessions, [key]: { ...selected, enabled } })} />
          <div className="min-w-0 flex-1">
            <div id={`${id}-${key}`} className="text-sm font-[640]">{label}</div>
            <div id={`${id}-${key}-range`} className="text-xs text-ink-3">
              {unavailable ? '当前渠道尚未验证盘后交易能力' : key === 'opening_locked' ? '09:20–09:24 可选 · 北京时间' : `固定 ${time} 触发 · 北京时间`}
            </div>
          </div>
          {key === 'opening_locked' && <MacTimePicker ariaLabel="早盘不可撤单阶段触发时间" value={selected.time}
            min="09:20" max="09:24" compact
            onChange={(time) => onChange({ ...sessions, [key]: { ...selected, time } })} />}
        </div>
      })}
    </div>
    <p className="text-xs text-ink-3">每个已开启阶段独立触发一次；09:25–09:30 仅等待已有订单。补发不跨阶段，账户忙时跳过。撤单受限时保留原委托，确认结束后才可换单。</p>
  </div>
}
