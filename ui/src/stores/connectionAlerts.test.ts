import { beforeEach, expect, test } from 'bun:test'
import { connectionAlertId, useConnectionAlerts } from './connectionAlerts'
import { connectionPresentation } from '@/features/account/connectionProblem'
import type { AccountConnectionDetail } from '@/types/api'

const failure = (id: string): AccountConnectionDetail => ({
  status: 'unavailable', observed_at: 'now', message: '账户未连接', error: { connection_event_id: id },
})

beforeEach(() => useConnectionAlerts.setState({ accountId: null, requestId: 0, seen: new Set() }))

test('one shared dialog per failure, closed failures stay dismissed across snapshots', () => {
  const store = useConnectionAlerts.getState
  store().observe({ 7: failure('first') })
  expect(store().accountId).toBe(7)
  store().close()
  store().observe({ 7: { ...failure('first'), observed_at: 'later' } })
  expect(store().accountId).toBeNull()
  store().observe({ 7: failure('second') })
  expect(store().accountId).toBe(7)
})

test('other accounts wait until the current dialog is closed', () => {
  const store = useConnectionAlerts.getState
  const frames = { 7: failure('a'), 8: failure('b') }
  store().observe(frames)
  store().observe(frames)
  expect(store().accountId).toBe(7)
  store().close()
  store().observe(frames)
  expect(store().accountId).toBe(8)
})

test('healthy, unknown and intentionally disconnected accounts do not pop up', () => {
  for (const status of ['ready', 'unknown', 'disconnected'] as const) {
    expect(connectionAlertId(7, { ...failure('old'), status })).toBeNull()
  }
})

test('channel action is allowlisted; executable paths are not accepted', () => {
  const result = connectionPresentation({ actions: [
    { label: '连接账户', intent: 'connect_account', path: '/arbitrary' },
    { label: '运行脚本', intent: 'execute_script' },
  ] })
  expect(result.actions).toEqual([{ label: '连接账户', intent: 'connect_account' }])
})
