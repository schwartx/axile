import { afterEach, expect, test } from 'bun:test'
import { useSimulationAccounts } from './simulationAccounts'
import type { SimulationAccount } from '@/types/api'

const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
const initial = useSimulationAccounts.getState()
afterEach(() => {
  useSimulationAccounts.setState(initial)
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else Reflect.deleteProperty(globalThis, 'localStorage')
})
const account: SimulationAccount = {
  account: { total_asset: 1000, available_cash: 1000, market_value: 0, currency: 'CNY', positions: [] },
  quotes: [], long_leverage: 1, short_leverage: 1,
}

test('各渠道独立保存并复制输入，持久化失败不覆盖已有配置', () => {
  let persisted = ''
  let failed = false
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    setItem(_key: string, value: string) { if (failed) throw new Error('quota'); persisted = value },
  } })
  const save = useSimulationAccounts.getState().save
  save('ctp', account)
  save('tq', { ...account, long_leverage: 2 })
  account.account.total_asset = 2000
  expect(useSimulationAccounts.getState().accounts.ctp?.account.total_asset).toBe(1000)
  expect(JSON.parse(persisted).tq.long_leverage).toBe(2)
  failed = true
  expect(() => save('ctp', account)).toThrow('当前草稿已保留')
  expect(useSimulationAccounts.getState().accounts.ctp?.account.total_asset).toBe(1000)
})
