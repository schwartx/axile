import { getCronDescription } from '@/features/setup/cronDescription'

/** 可视化编辑器的本地草稿；只生成原输入框使用的五段 cron。 */
export interface CronFieldDefinition {
  id: string
  label: string
  unit: string
  min: number
  max: number
  everyLabel: string
}

// 与 APScheduler.from_crontab 一致：周一为 0、周日为 6。
export const CRON_FIELDS: CronFieldDefinition[] = [
  { id: 'minute', label: '分钟', unit: '分', min: 0, max: 59, everyLabel: '每分钟' },
  { id: 'hour', label: '小时', unit: '时', min: 0, max: 23, everyLabel: '每小时' },
  { id: 'day', label: '日期', unit: '日', min: 1, max: 31, everyLabel: '每日' },
  { id: 'month', label: '月份', unit: '月', min: 1, max: 12, everyLabel: '每月' },
  { id: 'week', label: '星期', unit: '天', min: 0, max: 6, everyLabel: '每周' },
]

export type CronFieldMode = 'every' | 'interval' | 'range' | 'specific' | 'raw'

/** 切换设置方式时保留数值；未编辑的字段保留原文。 */
export interface CronFieldDraft {
  mode: CronFieldMode
  start: string
  intervalStart: string
  end: string
  step: string
  values: number[]
  raw: string
  original?: string
}

export interface CronRuleDraft {
  id: string
  raw: string
  fields: CronFieldDraft[] | null
}

export const DEFAULT_CRON_RULE = '0 9 * * *'
const WEEK_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const MONTH_LABELS = ['一月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '十一月', '十二月']
const WEEK_NAMES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const MONTH_NAMES = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

export function cronValueLabel(field: CronFieldDefinition, value: number): string {
  if (field.id === 'week') return WEEK_LABELS[value]!
  if (field.id === 'month') return MONTH_LABELS[value - 1]!
  return `${value}${field.unit}`
}

function numericExpression(field: CronFieldDefinition, expression: string): string {
  const names = field.id === 'week' ? WEEK_NAMES : field.id === 'month' ? MONTH_NAMES : []
  return expression.replace(/[a-z]+/gi, (name) => {
    const index = names.indexOf(name.toLowerCase())
    return index < 0 ? name : String(index + field.min)
  })
}

/** 校验数值、名称、范围、步长及混合列表，保留 APScheduler 的 last 日期语法。 */
function validFieldExpression(field: CronFieldDefinition, expression: string): boolean {
  return numericExpression(field, expression).split(',').every((part) => {
    if (field.id === 'day' && part.toLowerCase() === 'last') return true
    const match = /^(\*|\d+)(?:-(\d+))?(?:\/(\d+))?$/.exec(part)
    if (!match || (match[1] === '*' && match[2])) return false
    const start = match[1] === '*' ? field.min : Number(match[1])
    const end = match[2] !== undefined ? Number(match[2]) : match[1] === '*' || match[3] !== undefined ? field.max : start
    if (start < field.min || end > field.max || start > end) return false
    return match[3] === undefined || (Number(match[3]) >= 1 && Number(match[3]) <= end - start)
  })
}

export function formatCronField(field: CronFieldDefinition, draft: CronFieldDraft): string {
  if (draft.original !== undefined) return draft.original
  switch (draft.mode) {
    case 'every': return '*'
    case 'specific': return [...draft.values].sort((a, b) => a - b).join(',')
    case 'range': return draft.start === draft.end ? draft.start : `${draft.start}-${draft.end}`
    case 'interval': return `${/^\d+$/.test(draft.intervalStart) && Number(draft.intervalStart) === field.min ? '*' : draft.intervalStart}/${draft.step}`
    case 'raw': return draft.raw
  }
}

export function cronFieldError(field: CronFieldDefinition, draft: CronFieldDraft): string | null {
  if (draft.mode === 'specific' && !draft.values.length) return '请至少选择一项。'
  if (validFieldExpression(field, formatCronField(field, draft))) return null
  return `请检查${field.label}：取值为 ${field.min}–${field.max}，范围递增，间隔不能超过剩余跨度。`
}

function parseCronField(field: CronFieldDefinition, expression: string): CronFieldDraft {
  const draft: CronFieldDraft = {
    mode: 'raw', start: String(field.min), intervalStart: String(field.min), end: String(field.max), step: '1', values: [], raw: expression, original: expression,
  }
  if (!validFieldExpression(field, expression)) return draft
  if (expression === '*') return { ...draft, mode: 'every' }
  const numeric = numericExpression(field, expression)
  if (/^\d+(,\d+)*$/.test(numeric)) {
    return { ...draft, mode: 'specific', values: [...new Set(numeric.split(',').map(Number))].sort((a, b) => a - b) }
  }
  const range = /^(\d+)-(\d+)$/.exec(numeric)
  if (range) {
    if (field.id === 'week') {
      return { ...draft, mode: 'specific', values: Array.from({ length: Number(range[2]) - Number(range[1]) + 1 }, (_, index) => Number(range[1]) + index) }
    }
    return { ...draft, mode: 'range', start: range[1]!, end: range[2]! }
  }
  const interval = /^(\*|\d+)\/(\d+)$/.exec(expression)
  if (interval && field.id !== 'week') {
    return { ...draft, mode: 'interval', intervalStart: interval[1] === '*' ? String(field.min) : interval[1]!, step: interval[2]! }
  }
  // 混合列表、带结束值的步长等不强行映射，仍可在当前表达式输入框编辑。
  return draft
}

export function parseCronRule(raw: string, id: string = crypto.randomUUID()): CronRuleDraft {
  const parts = raw.trim().split(/\s+/)
  return { id, raw, fields: parts.length === CRON_FIELDS.length ? CRON_FIELDS.map((field, index) => parseCronField(field, parts[index]!)) : null }
}

export function parseCronSetting(raw: string): CronRuleDraft[] {
  const parts = raw.split('|').map((part) => part.trim()).filter(Boolean)
  return (parts.length ? parts : [DEFAULT_CRON_RULE]).map((part) => parseCronRule(part))
}

/** 手动输入保留原文；仅可视化操作时重建当前规则。 */
export function updateCronRuleField(rule: CronRuleDraft, index: number, draft: CronFieldDraft): CronRuleDraft {
  if (!rule.fields) return rule
  const fields = rule.fields.map((item, position) => position === index ? draft : item)
  return { ...rule, fields, raw: fields.map((item, position) => formatCronField(CRON_FIELDS[position]!, item)).join(' ') }
}

export function cronRuleError(rule: CronRuleDraft): string | null {
  if (!rule.fields) return '每条规则须包含 5 项（分、时、日、月、星期）。'
  for (const [index, draft] of rule.fields.entries()) {
    const error = cronFieldError(CRON_FIELDS[index]!, draft)
    if (error) return error
  }
  return null
}

export function describeCronRule(rule: CronRuleDraft): string {
  if (cronRuleError(rule)) return '无效的 CRON 表达式'
  const parts = rule.fields!.map((draft, index) => {
    const field = CRON_FIELDS[index]!
    return numericExpression(field, formatCronField(field, draft))
  })
  return getCronDescription(parts.join(' '))
}
