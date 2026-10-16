import { defaultClosePolicy } from '@/features/account/futuresClosePolicy'
import type { FuturesClosePolicy } from '@/types/api'
import { create } from 'zustand'
import { defaultExecutionTimeoutForChannel } from '@/features/account/executionTimeout'
import type { AlgorithmRef } from '@/features/setup/algorithms'
import { getChannelDescriptor } from '@/stores/channels'
import type { TradeChannel } from '@/types/api'
import {
  defaultNightOn,
  defaultScheduleRule,
  DEFAULT_PRESET,
  type ScheduleKind,
  type ScheduleRule,
  type TimerTab,
} from '@/features/setup/cron'

/** 账户流草稿。 */
export interface AccountDraft {
  name: string
  channel: TradeChannel
  /** 连接表单（渠道特定字段）。 */
  config: Record<string, unknown>
  portfolioId: number | null
  /** 主交易算法引用（`{method, params}`）；由「怎么交易」步的算法编辑器产出。 */
  algorithm: AlgorithmRef
  futuresClosePolicy: FuturesClosePolicy
  longLeverage: string
  shortLeverage: string
  executionTimeout: string
  autoOn: boolean
  /** 快捷 tab 选中的预设 id（可多选，高级组合后较少使用）。 */
  presetIds: string[]
  /** 是否把渠道声明的夜盘时点并入快捷节奏。 */
  nightOn: boolean
  supN: number
  supM: number
  rawCron: string
  /** 定时 UI：快捷 | 高级 | 自定义。 */
  timerTab: TimerTab
  /** 高级：时间规则；length≥2 时显示右栏组合列表。 */
  scheduleRules: ScheduleRule[]
  /** 高级当前编辑的规则 id。 */
  selectedRuleId: string
}

interface WizardState {
  acct: AccountDraft
  setAcct: (patch: Partial<AccountDraft>) => void
  resetAcct: () => void
}

/** 返回渠道对应的账户杠杆默认值。 */
export function defaultLeveragesForChannel(channel: TradeChannel) {
  const defaults = getChannelDescriptor(channel)?.defaults
  return {
    longLeverage: String(defaults?.long_leverage ?? 1),
    shortLeverage: String(defaults?.short_leverage ?? 1),
  }
}

export function initialTimerForSchedule(schedule: ScheduleKind) {
  const rule = defaultScheduleRule(schedule)
  return {
    autoOn: true,
    presetIds: [DEFAULT_PRESET[schedule]] as string[],
    nightOn: defaultNightOn(schedule),
    supN: 2,
    supM: 1,
    rawCron: '',
    timerTab: 'quick' as TimerTab,
    scheduleRules: [rule],
    selectedRuleId: rule.id,
  }
}

const initialAcct: AccountDraft = {
  name: '',
  channel: '',
  config: {},
  portfolioId: null,
  algorithm: { method: '', params: {} },
  futuresClosePolicy: defaultClosePolicy(),
  ...defaultLeveragesForChannel(''),
  executionTimeout: defaultExecutionTimeoutForChannel(''),
  autoOn: true,
  presetIds: [],
  nightOn: false,
  supN: 2,
  supM: 1,
  rawCron: '',
  timerTab: 'quick',
  scheduleRules: [],
  selectedRuleId: '',
}

/** 建号向导的跨步骤草稿。组合新建不走向导：列表页弹层创建后直接进编辑器。 */
export const useWizardStore = create<WizardState>((set) => ({
  acct: { ...initialAcct },
  setAcct: (patch) => set((s) => ({ acct: { ...s.acct, ...patch } })),
  resetAcct: () => set({ acct: { ...initialAcct } }),
}))
