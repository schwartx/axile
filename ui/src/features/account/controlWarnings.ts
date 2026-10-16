import type { ControlFrame } from '@/stores/liveControl'
import { quotaWarningCount } from '@/features/account/quotaWarning'

export type QuotaWarningStage = 'warning' | 'exhausted'
export interface QuotaWarning {
  id: string
  accountId: number
  operation: 'place_order' | 'cancel_order'
  stage: QuotaWarningStage
  used: number
  limit: number
  remaining: number
  percent: number | null
  stale: boolean
}

export const quotaOperationName = (operation: QuotaWarning['operation']) => operation === 'place_order' ? '报单' : '撤单'
export const quotaStageName = (stage: QuotaWarningStage) => stage === 'exhausted' ? '额度已用尽' : '额度预警'

function record(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === 'object' && !Array.isArray(value)
}

function nonnegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function warningPercent(value: unknown): number | null {
  return nonnegativeInteger(value) && value >= 1 && value <= 99 ? value : null
}

interface OperationQuotaState { known: boolean; warning: QuotaWarning | null }
function operationQuotaState(frame: ControlFrame, operation: QuotaWarning['operation'], config: unknown, reliable: boolean): OperationQuotaState {
  if (!record(config)) return { known: true, warning: null }
  const rule = [...frame.rules, ...frame.metrics].find(item => item.operation === operation && item.scope === 'account' && item.rule === 'per_day')
  if (!rule) return { known: false, warning: null }
  if (rule.limit == null || rule.limit === 0) return { known: true, warning: null }
  if (!nonnegativeInteger(rule.limit) || !nonnegativeInteger(rule.value)) return { known: false, warning: null }
  const percent = warningPercent(config.daily_warning_percent)
  const stage = rule.value >= rule.limit ? 'exhausted'
    : percent != null && rule.value >= quotaWarningCount(rule.limit, percent) ? 'warning' : null
  if (!stage) return { known: true, warning: null }
  return { known: true, warning: {
    id: JSON.stringify([frame.account_id, frame.timezone, frame.control_date, operation]),
    accountId: frame.account_id, operation, stage, used: rule.value, limit: rule.limit,
    remaining: Math.max(0, rule.limit - rule.value), percent, stale: !reliable,
  } }
}

/** 不回退运行实例的配置，不从柜台结果或页面编辑草稿推测额度。 */
export function controlQuotaState(frame: ControlFrame | undefined): { reliable: boolean; warnings: QuotaWarning[] } {
  if (!frame) return { reliable: false, warnings: [] }
  const reliable = !frame.stale && !frame.incomplete && ['running', 'idle'].includes(frame.status)
  const policy = frame.status === 'idle' ? frame.saved_policy : frame.actual_policy
  const operations = policy?.operations
  if (!record(operations) || !frame.control_date || !frame.timezone) {
    return { reliable: false, warnings: [] }
  }
  const states = (['place_order', 'cancel_order'] as const).map(operation => operationQuotaState(frame, operation, operations[operation], reliable))
  return {
    reliable: reliable && states.every(state => state.known),
    warnings: states.flatMap(state => state.warning ? [state.warning] : []),
  }
}

export const QUOTA_REMINDERS_KEY = 'axile:quota-reminders:v1'
type ReminderStorage = Pick<Storage, 'getItem' | 'setItem'>

/** 持久化已展示阶段；写入失败时同一会话仍去重。 */
export class QuotaReminderLedger {
  private seen: Record<string, number> = {}
  constructor(private storage: () => ReminderStorage | null) {}

  claim(warning: QuotaWarning): boolean {
    let target: ReminderStorage | null = null
    try {
      target = this.storage()
      const raw: unknown = JSON.parse(target?.getItem(QUOTA_REMINDERS_KEY) ?? '{}')
      if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
        for (const [key, value] of Object.entries(raw)) {
          if (value === 1 || value === 3) this.seen[key] = (this.seen[key] ?? 0) | value
        }
      }
    } catch { /* 不可用的浏览器存储降级为会话记录。 */ }
    const mask = warning.stage === 'exhausted' ? 3 : 1
    if (((this.seen[warning.id] ?? 0) & mask) === mask) return false
    this.seen[warning.id] = (this.seen[warning.id] ?? 0) | mask
    try { target?.setItem(QUOTA_REMINDERS_KEY, JSON.stringify(this.seen)) } catch { /* 保留内存记录。 */ }
    return true
  }
}

export interface QuotaWarningEntry extends QuotaWarning { order: number }
interface WarningControllerOptions {
  frames: () => Record<number, ControlFrame>
  visible: () => boolean
  ledger: QuotaReminderLedger
  /** Web Locks 串行领取；不支持的浏览器直接运行。 */
  exclusive: (run: () => void) => Promise<void>
  changed: (entries: QuotaWarningEntry[], announced: QuotaWarning[]) => void
}

/** 跨快照保留待关闭条目；领取时重新读取最新帧，避免异步锁使用旧计数。 */
export class QuotaWarningController {
  private entries = new Map<string, QuotaWarningEntry>()
  private order = 0
  private active = true
  constructor(private options: WarningControllerOptions) {}

  dispose() { this.active = false }
  dismiss(id?: string) {
    if (id) this.entries.delete(id)
    else this.entries.clear()
    this.publish([])
  }

  async update() {
    if (!this.active) return
    this.reconcile()
    this.publish([])
    if (!this.options.visible()) return
    await this.options.exclusive(() => {
      if (!this.active || !this.options.visible()) return
      this.reconcile()
      const announced: QuotaWarning[] = []
      for (const frame of Object.values(this.options.frames())) {
        const state = controlQuotaState(frame)
        if (!state.reliable) continue
        for (const warning of state.warnings) {
          if (!this.options.ledger.claim(warning)) continue
          this.entries.set(warning.id, { ...warning, order: ++this.order })
          announced.push(warning)
        }
      }
      this.publish(announced)
    })
  }

  private reconcile() {
    const frames = this.options.frames()
    for (const [id, entry] of this.entries) {
      const frame = frames[entry.accountId]
      if (!frame || (frame.control_date && frame.timezone && id !== JSON.stringify([frame.account_id, frame.timezone, frame.control_date, entry.operation]))) {
        this.entries.delete(id)
        continue
      }
      const state = controlQuotaState(frame)
      const next = state.warnings.find(item => item.id === id)
      if (state.reliable && !next) this.entries.delete(id)
      else if (state.reliable && next) {
        this.entries.set(id, { ...next, order: entry.order })
      } else this.entries.set(id, { ...entry, stale: true })
    }
  }

  private publish(announced: QuotaWarning[]) {
    if (!this.active) return
    const entries = [...this.entries.values()].sort((a, b) => Number(b.stage === 'exhausted') - Number(a.stage === 'exhausted') || b.order - a.order)
    this.options.changed(entries, announced)
  }
}
