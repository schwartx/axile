import { parseDay } from '@/components/ui/datePickerModel'

export type TransferPlan = {
  start: string | null; end: string | null; source_url: string; every_hours: number
  execution_days: number | null; replay_days: number | null
}
export type TransferTime = { date: string; time: string; original?: string }

function timeDraft(original: string | null | undefined): TransferTime {
  if (!original) return { date: '', time: '' }
  const local = new Date(Date.parse(original) + 8 * 3600000).toISOString()
  return { date: local.slice(0, 10), time: local.slice(11, 16), original }
}

export function transferDraft(plan: Partial<TransferPlan> = {}) {
  return {
    source_url: plan.source_url ?? '',
    start: timeDraft(plan.start), end: timeDraft(plan.end),
    automatic: (plan.every_hours ?? 0) > 0,
    every_hours: String(plan.every_hours || 1),
    execution_days: plan.execution_days?.toString() ?? '',
    replay_days: plan.replay_days?.toString() ?? '',
  }
}
export type TransferDraft = ReturnType<typeof transferDraft>

function timestamp(value: TransferTime, label: string): string | null {
  if (!value.date && !value.time) return null
  if (parseDay(value.date) == null || value.date < '0001-01-01' || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value.time))
    throw new Error(`请填写完整有效的${label}（北京时间）`)
  // 更换控件不应截断已经保存的秒和小数秒。
  if (value.original) {
    const previous = timeDraft(value.original)
    if (previous.date === value.date && previous.time === value.time) return value.original
  }
  return `${value.date}T${value.time}:00+08:00`
}

export function positiveInteger(value: string, max = Number.MAX_SAFE_INTEGER): boolean {
  return value.trim() !== '' && Number.isSafeInteger(Number(value)) && Number(value) >= 1 && Number(value) <= max
}

export function transferPlan(draft: TransferDraft, range: boolean): TransferPlan {
  const start = range ? timestamp(draft.start, '开始时间') : null
  const end = range ? timestamp(draft.end, '结束时间') : null
  if (start && end && Date.parse(start) >= Date.parse(end)) throw new Error('结束时间必须晚于开始时间')
  if (draft.automatic && !positiveInteger(draft.every_hours, 8760)) throw new Error('拉取间隔需为 1–8760 的整数小时')
  if (draft.automatic && !draft.source_url.trim()) throw new Error('自动拉取需要填写来源端地址')
  for (const [label, value] of [['执行记录', draft.execution_days], ['回放', draft.replay_days]]) {
    if (value.trim() && !positiveInteger(value)) throw new Error(`${label}保留天数需为正整数，留空则不清理`)
  }
  return {
    source_url: draft.source_url.trim(), start, end,
    every_hours: draft.automatic ? Number(draft.every_hours) : 0,
    execution_days: draft.execution_days.trim() ? Number(draft.execution_days) : null,
    replay_days: draft.replay_days.trim() ? Number(draft.replay_days) : null,
  }
}
