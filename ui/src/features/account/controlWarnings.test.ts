import { describe, expect, it } from 'bun:test'
import type { ControlFrame } from '@/stores/liveControl'
import {
  controlQuotaState, QuotaReminderLedger, QuotaWarningController,
  type QuotaWarning, type QuotaWarningEntry,
} from './controlWarnings'

function warningFrame(used = 80, overrides: Partial<ControlFrame> = {}): ControlFrame {
  return {
    account_id: 1, execution_id: 'e1', instance_id: 'i1', status: 'running', seq: 1,
    sampled_at_ms: 1000, stale: false, incomplete: false, pending_policy: false,
    timezone: 'Asia/Shanghai', control_date: '2026-10-16',
    actual_policy: { operations: { cancel_order: { daily_warning_percent: 80 } } },
    saved_policy: { operations: { cancel_order: { daily_warning_percent: 90 } } },
    rules: [{ id: 'account:cancel_order:per_day', operation: 'cancel_order', scope: 'account', symbol: null,
      rule: 'per_day', value: used, limit: 100, remaining: Math.max(0, 100 - used), estimated_wait_ms: 0, on_trigger: 'block' }],
    metrics: [], waiting_total: 0, waiting: [], records: [], ...overrides,
  }
}

function memoryStorage() {
  const values = new Map<string, string>()
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) } }
}

describe('额度预警状态', () => {
  it('使用运行实例规则和向上取整，忽略未生效的保存配置', () => {
    expect(controlQuotaState(warningFrame(79)).warnings).toEqual([])
    expect(controlQuotaState(warningFrame()).warnings[0].stage).toBe('warning')
    expect(controlQuotaState(warningFrame(80, { status: 'idle' })).warnings).toEqual([])
    expect(controlQuotaState(warningFrame(90, { status: 'idle' })).warnings[0].stage).toBe('warning')
    expect(controlQuotaState(warningFrame(100, { actual_policy: null })).reliable).toBe(false)
    const small = warningFrame(2)
    small.rules[0].limit = 3
    expect(controlQuotaState(small).warnings).toEqual([])
    small.rules[0].value = 3
    expect(controlQuotaState(small).warnings[0].stage).toBe('exhausted')
  })

  it('关闭比例后仍识别用尽，无限额和零额度不预警', () => {
    const noWarning = { operations: { cancel_order: {} } }
    expect(controlQuotaState(warningFrame(90, { actual_policy: noWarning })).warnings).toEqual([])
    expect(controlQuotaState(warningFrame(101, { actual_policy: noWarning })).warnings[0]).toMatchObject({ stage: 'exhausted', remaining: 0 })
    for (const limit of [null, 0]) {
      const frame = warningFrame(100)
      frame.rules[0].limit = limit
      expect(controlQuotaState(frame).warnings).toEqual([])
    }
  })

  it('只判断账户每日报单、撤单，不叠加原生撤单、品种和分钟计数', () => {
    const frame = warningFrame(10)
    frame.rules.push({ ...frame.rules[0], scope: 'symbol', value: 100 },
      { ...frame.rules[0], operation: 'cancel_order_ctp', value: 100 },
      { ...frame.rules[0], rule: 'per_minute', value: 100 })
    expect(controlQuotaState(frame).warnings).toEqual([])
  })

  it('未知计数和无效配置不会制造提醒，陈旧或不完整不允许领取', () => {
    for (const overrides of [{ stale: true }, { incomplete: true }, { status: 'unknown' }, { status: 'disabled' }, { control_date: null }]) {
      expect(controlQuotaState(warningFrame(100, overrides)).reliable).toBe(false)
    }
    for (const raw of [null, {}, { operations: null }, { operations: { cancel_order: null } }]) {
      expect(controlQuotaState(warningFrame(100, { actual_policy: raw as ControlFrame['actual_policy'] })).warnings).toEqual([])
    }
    const frame = warningFrame()
    frame.rules[0].value = null
    expect(controlQuotaState(frame)).toEqual({ reliable: false, warnings: [] })
  })
})

describe('持久化提醒阶段', () => {
  it('刷新和不同标签页共享记录，直接用尽同时消耗两个阶段', () => {
    const storage = memoryStorage()
    const warning = controlQuotaState(warningFrame()).warnings[0]
    const ledger = new QuotaReminderLedger(() => storage)
    expect(ledger.claim(warning)).toBe(true)
    expect(new QuotaReminderLedger(() => storage).claim(warning)).toBe(false)
    const exhausted = { ...warning, stage: 'exhausted' as const }
    expect(ledger.claim(exhausted)).toBe(true)
    expect(new QuotaReminderLedger(() => storage).claim(exhausted)).toBe(false)
    const nextDay = { ...exhausted, id: 'next-day' }
    expect(ledger.claim(nextDay)).toBe(true)
    expect(ledger.claim({ ...nextDay, stage: 'warning' })).toBe(false)
  })

  it('损坏或不可用存储降级为会话去重', () => {
    const warning = controlQuotaState(warningFrame()).warnings[0]
    for (const storage of [() => { throw new Error('denied') }, () => ({ getItem: () => '{', setItem: () => { throw new Error('full') } })]) {
      const ledger = new QuotaReminderLedger(storage)
      expect(ledger.claim(warning)).toBe(true)
      expect(ledger.claim(warning)).toBe(false)
    }
  })
})

function setup(storage = memoryStorage(), exclusive: (run: () => void) => Promise<void> = async run => run()) {
  const state = { frames: { 1: warningFrame() } as Record<number, ControlFrame>, visible: true, entries: [] as QuotaWarningEntry[], announced: [] as QuotaWarning[] }
  const controller = new QuotaWarningController({
    frames: () => state.frames, visible: () => state.visible,
    ledger: new QuotaReminderLedger(() => storage), exclusive,
    changed: (entries, announced) => { state.entries = entries; state.announced.push(...announced) },
  })
  return { state, controller }
}

describe('全局预警生命周期', () => {
  it('首次补弹，关闭不再弹，用尽再次弹，重连和实例切换不重置记录', async () => {
    const { state, controller } = setup()
    await controller.update()
    expect(state.entries).toHaveLength(1)
    controller.dismiss()
    state.frames[1] = warningFrame(85, { instance_id: 'i2' })
    await controller.update()
    expect(state.entries).toHaveLength(0)
    state.frames[1] = warningFrame(100)
    await controller.update()
    expect(state.entries[0].stage).toBe('exhausted')
    await controller.update()
    expect(state.announced.map(item => item.stage)).toEqual(['warning', 'exhausted'])
  })

  it('合并多账户，按用尽和新条目排序，单条关闭不影响其他条目', async () => {
    const { state, controller } = setup()
    state.frames[2] = warningFrame(100, { account_id: 2 })
    await controller.update()
    expect(state.entries.map(item => item.accountId)).toEqual([2, 1])
    controller.dismiss(state.entries[0].id)
    expect(state.entries.map(item => item.accountId)).toEqual([1])
  })

  it('陈旧状态保留最后用量，恢复正常、换日和账户移除时清理', async () => {
    const { state, controller } = setup()
    await controller.update()
    state.frames[1] = warningFrame(99, { stale: true })
    await controller.update()
    expect(state.entries[0]).toMatchObject({ used: 80, stale: true })
    state.frames[1] = warningFrame(70)
    await controller.update()
    expect(state.entries).toHaveLength(0)
    state.frames[1] = warningFrame(90, { control_date: '2026-10-17' })
    await controller.update()
    expect(state.entries).toHaveLength(1)
    state.frames = {}
    await controller.update()
    expect(state.entries).toHaveLength(0)
  })

  it('隐藏标签页不领取；锁串行领取且使用等待后最新快照', async () => {
    let queue = Promise.resolve()
    const exclusive = (run: () => void) => { queue = queue.then(run); return queue }
    const storage = memoryStorage()
    const first = setup(storage, exclusive), second = setup(storage, exclusive)
    first.state.visible = false
    await Promise.all([first.controller.update(), second.controller.update()])
    expect(first.state.entries).toHaveLength(0)
    expect(second.state.entries).toHaveLength(1)
    first.state.visible = true
    await first.controller.update()
    expect(first.state.entries).toHaveLength(0)
    const pending = first.controller.update()
    first.state.frames[1] = warningFrame(100)
    await pending
    expect(first.state.entries[0].stage).toBe('exhausted')
  })

  it('卸载后等待中的领取不写入提醒记录', async () => {
    let run: (() => void) | undefined
    const { state, controller } = setup(memoryStorage(), async callback => { run = callback })
    await controller.update()
    controller.dispose()
    run?.()
    expect(state.announced).toHaveLength(0)
  })
})
