import { create } from 'zustand'
import type { SimulationAccount } from '@/types/api'

const storageKey = 'axile.simulation-accounts.v1'

function readAccounts(): Record<string, SimulationAccount> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(storageKey) ?? '{}')
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
    return Object.fromEntries(Object.entries(value).filter(([, item]) =>
      item && typeof item === 'object' && item.account && typeof item.account.total_asset === 'number'
      && typeof item.account.available_cash === 'number' && typeof item.account.currency === 'string'
      && Array.isArray(item.account.positions) && item.account.positions.every((position: unknown) =>
        position && typeof position === 'object' && 'symbol' in position && typeof position.symbol === 'string')
      && Array.isArray(item.quotes) && item.quotes.every((quote: unknown) =>
        quote && typeof quote === 'object' && 'symbol' in quote && typeof quote.symbol === 'string')
      && typeof item.long_leverage === 'number' && typeof item.short_leverage === 'number'))
  } catch {
    return {}
  }
}

interface SimulationAccountsState {
  accounts: Record<string, SimulationAccount>
  save: (channel: string, value: SimulationAccount) => void
}

/** 每个渠道的试跑配置只保存在本浏览器；存储失败不冒充保存成功。 */
export const useSimulationAccounts = create<SimulationAccountsState>((set, get) => ({
  accounts: readAccounts(),
  save: (channel, value) => {
    const accounts = { ...get().accounts, [channel]: structuredClone(value) }
    try {
      localStorage.setItem(storageKey, JSON.stringify(accounts))
    } catch {
      throw new Error('无法保存模拟账户，请允许浏览器存储后重试；当前草稿已保留。')
    }
    set({ accounts })
  },
}))
