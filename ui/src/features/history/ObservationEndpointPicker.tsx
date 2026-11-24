import { useEffect, useMemo, useState } from 'react'
import { DatePicker } from '@/components/ui/DatePicker'
import { Select, type SelectOption } from '@/components/ui/Select'
import { timeLabel } from './chartModel'

export function ObservationEndpointPicker({ label, value, other, times, onChange }: {
  label: string
  value: number | undefined
  other: number | undefined
  times: number[]
  onChange: (time: number) => void
}) {
  const dates = useMemo(() => {
    const groups = new Map<string, SelectOption<number>[]>()
    for (const time of times) {
      if (time === other) continue
      const label = timeLabel(time), day = label.slice(0, 10)
      const options = groups.get(day) ?? []
      options.push({ value: time, label: label.slice(11) })
      groups.set(day, options)
    }
    return groups
  }, [times, other])
  const [draftDay, setDraftDay] = useState<string | null>(null)
  useEffect(() => { setDraftDay(null) }, [times, value, other])
  const committed = value == null ? '' : timeLabel(value)
  const day = draftDay ?? committed.slice(0, 10)
  const options = dates.get(day) ?? []

  return <div className="flex min-w-0 items-center gap-2" role="group" aria-label={label}>
    <span className="shrink-0 text-ink-3">{label}</span>
    <div className="flex min-w-0 items-center rounded-lg border border-line bg-surface pr-1 text-ink-2">
      <DatePicker ariaLabel={'比较' + label + '日期'} value={day} availableDays={[...dates.keys()]} className="shrink-0 border-0 bg-transparent whitespace-nowrap [&>button]:h-8"
        onChange={day => {
          const options = dates.get(day)!
          if (options.length === 1) {
            setDraftDay(null)
            onChange(options[0].value)
          } else setDraftDay(day === committed.slice(0, 10) ? null : day)
        }} />
      {options.length > 1
        ? <Select<number | null> ariaLabel={'比较' + label + '时刻'} appearance="inline" className="num h-8 gap-1! px-1 text-xs"
          placeholder="选时刻" value={draftDay ? null : value ?? null} options={options}
          onChange={time => { if (time != null) { setDraftDay(null); onChange(time) } }} />
        : <span className="num px-1 text-xs whitespace-nowrap">{committed.slice(11)}</span>}
    </div>
  </div>
}
