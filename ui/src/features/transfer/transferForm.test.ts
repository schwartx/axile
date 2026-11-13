import { expect, test } from 'bun:test'
import { transferDraft, transferPlan } from '@/features/transfer/transferForm'

test('保留已保存的时间精度，日期时刻统一以北京时间回显', () => {
  const start = '2026-11-12T17:23:45.123456Z'
  const draft = transferDraft({ start, source_url: 'https://source.test', every_hours: 6, execution_days: 7 })
  expect(draft.start).toMatchObject({ date: '2026-11-13', time: '01:23' })
  expect(transferPlan(draft, true)).toMatchObject({ start, end: null, every_hours: 6, execution_days: 7, replay_days: null })
  draft.start.time = '02:30'
  expect(transferPlan(draft, true).start).toBe('2026-11-13T02:30:00+08:00')
})

test('空边界与全部模式保持开放范围，非法日期和颠倒范围阻止提交', () => {
  const draft = transferDraft()
  expect(transferPlan(draft, true)).toMatchObject({ start: null, end: null })
  draft.start = { date: '2026-02-29', time: '09:00' }
  expect(() => transferPlan(draft, true)).toThrow('开始时间')
  draft.start = { date: '2024-02-29', time: '' }
  expect(() => transferPlan(draft, true)).toThrow('开始时间')
  draft.start.time = '24:00'
  expect(() => transferPlan(draft, true)).toThrow('开始时间')
  draft.start.time = '09:00'
  draft.end = { date: '2024-02-29', time: '08:00' }
  expect(() => transferPlan(draft, true)).toThrow('结束时间必须晚于开始时间')
  expect(transferPlan(draft, false)).toMatchObject({ start: null, end: null })
})

test('默认关闭自动拉取且不清理，正整数草稿才可提交', () => {
  const draft = transferDraft()
  expect(transferPlan(draft, false)).toMatchObject({ every_hours: 0, execution_days: null, replay_days: null })
  draft.automatic = true
  for (const value of ['', '0', '1.5', 'invalid', '8761']) {
    draft.every_hours = value
    expect(() => transferPlan(draft, false)).toThrow('拉取间隔')
  }
  draft.every_hours = '6'
  expect(() => transferPlan(draft, false)).toThrow('来源端地址')
  draft.source_url = 'https://source.test'
  for (const value of ['0', '-1', '1.5', 'invalid']) {
    draft.execution_days = value
    expect(() => transferPlan(draft, false)).toThrow('保留天数')
  }
  draft.execution_days = '30'
  expect(transferPlan(draft, false)).toMatchObject({ every_hours: 6, execution_days: 30 })
  draft.automatic = false
  draft.execution_days = ''
  expect(transferPlan(draft, false)).toMatchObject({ every_hours: 0, execution_days: null })
})
