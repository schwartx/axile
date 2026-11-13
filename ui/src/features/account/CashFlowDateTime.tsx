import { useId } from 'react'
import { DatePicker } from '@/components/ui/DatePicker'
import { cashFlowTimeError } from '@/features/account/cashFlowTime'
import { formatBeijingDate } from '@/lib/scheduleTime'

const PARTS = [
  { label: '时', max: 23, pattern: '(?:[01]?[0-9]|2[0-3])' },
  { label: '分', max: 59, pattern: '[0-5]?[0-9]' },
  { label: '秒', max: 59, pattern: '[0-5]?[0-9]' },
] as const

/** 补录流水以键盘填写为主；日期与时分秒共同表达一个北京时间。 */
export function CashFlowDateTime({ date, time, onDateChange, onTimeChange, now = Date.now() }: {
  date: string
  time: string
  onDateChange: (value: string) => void
  onTimeChange: (value: string) => void
  now?: number
}) {
  const hintId = useId()
  const error = cashFlowTimeError(date, time, now)
  const parts = time.split(':')
  const edit = (index: number, value: string) => {
    onTimeChange(PARTS.map((_, i) => i === index ? value : parts[i] ?? '').join(':'))
  }
  return <div role="group" aria-label="发生时间（北京时间）" aria-invalid={!!error || undefined} className="grid min-w-0 gap-y-1.5 sm:col-span-2 sm:grid-cols-subgrid sm:items-start sm:gap-x-5">
    <div className="text-xs text-ink-2 sm:pt-2">发生时间<span aria-hidden="true" className="sm:hidden"> · </span><span className="text-ink-3 sm:mt-0.5 sm:block">北京时间</span></div>
    <div className="flex min-w-0 flex-wrap items-stretch gap-2">
      <DatePicker value={date} onChange={onDateChange} ariaLabel="发生日期（北京时间）" required
        max={formatBeijingDate(new Date(now).toISOString())} describedBy={hintId}
        className="flex-[1_1_calc(10ch+4rem)]" />
      <div role="group" aria-label="时分秒" className="inline-flex shrink-0 items-center gap-0.5 rounded-lg border border-line bg-surface px-2 text-[13px] text-ink-1 contrast:border-border-strong focus-within:border-accent">
        {PARTS.map(({ label, max, pattern }, index) => <span key={label} className="inline-flex items-center gap-0.5">
          {index > 0 && <span aria-hidden="true" className="text-ink-3">:</span>}
          <input type="text" inputMode="numeric" size={2} maxLength={2} required pattern={pattern}
            aria-label={`发生时间：${label}（北京时间）`} title={`${label}：00–${max}`}
            aria-describedby={hintId} aria-invalid={!!error || undefined}
            placeholder={label} value={parts[index] ?? ''} autoComplete="off" spellCheck={false}
            className="num box-content w-[2ch] rounded border-0 bg-transparent px-1 py-2 text-center text-ink-1 outline-none placeholder:text-ink-3 focus-visible:bg-fill focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
            onChange={event => edit(index, event.target.value)}
            onFocus={event => event.currentTarget.select()}
            onBlur={event => {
              if (event.currentTarget.value && event.currentTarget.validity.valid) edit(index, event.currentTarget.value.padStart(2, '0'))
            }}
            onKeyDown={event => {
              if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
              event.preventDefault()
              const next = Math.max(0, Math.min(max, (Number(parts[index]) || 0) + (event.key === 'ArrowUp' ? 1 : -1)))
              edit(index, String(next).padStart(2, '0'))
            }} />
        </span>)}
      </div>
      <p id={hintId} aria-live="polite" className={`w-full min-h-4 text-xs leading-4 ${error ? 'text-warn' : 'text-ink-3'}`}>
        {error ?? ''}
      </p>
    </div>
  </div>
}
