import type { ActivityTab } from '@/features/account/accountActivityModel'

export interface ActivityPosition { top: number; left: number }
export interface ActivityView {
  tab: ActivityTab
  symbol: string
  limit: number
  positions: Map<string, ActivityPosition>
}

// 仅保存查看状态，不缓存交易数据；页面刷新后自然清空。
const views = new Map<number, ActivityView>()
export function activityView(accountId: number): ActivityView {
  let view = views.get(accountId)
  if (!view) {
    view = { tab: 'pending', symbol: '', limit: 100, positions: new Map() }
    views.set(accountId, view)
  }
  return view
}
export function rememberActivityView(accountId: number, view: ActivityView) {
  views.set(accountId, view)
}
export function activityPositionKey(tab: ActivityTab, symbol: string) {
  return JSON.stringify([tab, tab === 'pending' ? '' : symbol])
}
