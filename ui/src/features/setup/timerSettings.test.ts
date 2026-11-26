import { expect, test } from 'bun:test'
import { defaultGMSessions } from './cron'
import { timerSettingsChange } from './timerSettings'

const cronExpr = '18 9 * * 1-5'
const paused = { cronExpr: '', autoOn: false, supplement: null, gmSessions: null }

test('信号暂停保留已存节奏，再启动时复用同一节奏', () => {
  expect(timerSettingsChange(true, 'ctp', { cronExpr, isStarted: true }, paused)).toEqual({
    patch: { is_started: false }, message: '已暂停',
  })
  expect(timerSettingsChange(true, 'ctp', { cronExpr, isStarted: false }, { ...paused, cronExpr, autoOn: true })).toEqual({
    patch: { is_started: true }, message: '已启动',
  })
})

test('信号暂停时修改 GM 阶段不清空节奏', () => {
  const gmSessions = defaultGMSessions()
  gmSessions.opening_locked = { enabled: true, time: '09:22' }
  expect(timerSettingsChange(true, 'gm', { cronExpr, isStarted: false }, { ...paused, gmSessions })).toEqual({
    patch: { channel_options: { gm: gmSessions } }, message: '节奏已更新',
  })
})

test('交易账户关闭定时节奏时同时清除补发', () => {
  const current = { cronExpr, isStarted: true, supplement: { count: 2, interval_minutes: 1 } }
  expect(timerSettingsChange(false, 'ctp', current, paused)).toEqual({
    patch: { cron_expr: '', supplement: null }, message: '已关闭自动调仓节奏',
  })
})
