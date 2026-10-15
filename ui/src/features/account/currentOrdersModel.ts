import type { CancelReceipt, CurrentOrder, CurrentOrdersSnapshot } from '@/lib/api/orders'

export function orderOffset(offset: string | null): string {
  return ({ '0': '开仓', '1': '平仓', '3': '平今', '4': '平昨', OPEN: '开仓', CLOSE: '平仓', CLOSETODAY: '平今', CLOSEYESTERDAY: '平昨' } as Record<string, string>)[offset ?? ''] ?? '开平未知'
}

export function orderDirection(direction: string): string {
  return direction === 'BUY' || direction === '买入' ? '买入' : direction === 'SELL' || direction === '卖出' ? '卖出' : '方向未知'
}

export function orderSummary(snapshot: CurrentOrdersSnapshot | null, stale: boolean): string {
  if (!snapshot || !snapshot.complete || snapshot.count == null || stale) return '在途委托 · 待确认'
  return `在途委托 · ${snapshot.count}`
}

export function currentOrderKey(order: Pick<CurrentOrder, 'symbol' | 'order_id'>): string {
  return JSON.stringify([order.symbol, order.order_id])
}

export function mergeReceipts(server: CancelReceipt[], local: CancelReceipt[]): CancelReceipt[] {
  const merged = new Map<string, CancelReceipt>()
  for (const receipt of [...local, ...server]) {
    const key = currentOrderKey(receipt)
    const previous = merged.get(key)
    // 渠道确认的订单终态跨请求生效；请求创建时间不能表示终态的观测先后。
    if (receipt.state === 'terminal') { merged.set(key, receipt); continue }
    if (previous?.state === 'terminal') continue
    if (previous?.request_id === receipt.request_id && previous.state === 'failed' && receipt.state !== 'failed') continue
    if (previous?.request_id === receipt.request_id) { merged.set(key, receipt); continue }
    if (previous && previous.request_id !== receipt.request_id && ['submitting', 'unknown'].includes(previous.state)) continue
    // 非终态仍按请求隔离，旧请求的失败不能解除新请求的不确定性。
    if (!previous || receipt.requested_at >= previous.requested_at) merged.set(key, receipt)
  }
  return [...merged.values()]
}

/** 保存本地操作的最新进展，已确认的订单终态及同一请求的失败不被迟到响应降级。 */
export function updateLocalReceipt(local: CancelReceipt[], receipt: CancelReceipt): CancelReceipt[] {
  const key = currentOrderKey(receipt)
  const previous = local.find(value => currentOrderKey(value) === key)
  const resolved = (previous?.state === 'terminal' && receipt.state !== 'terminal')
    || (previous?.request_id === receipt.request_id && previous.state === 'failed' && !['failed', 'terminal'].includes(receipt.state))
  return [...local.filter(value => currentOrderKey(value) !== key), resolved ? previous : receipt]
}

/** 只回写本页操作过的订单，避免服务端最近回执被淘汰后复活本地未知状态。 */
export function reconcileLocalReceipts(server: CancelReceipt[], local: CancelReceipt[]): CancelReceipt[] {
  const merged = new Map(mergeReceipts(server, local).map(receipt => [currentOrderKey(receipt), receipt]))
  return local.map(receipt => merged.get(currentOrderKey(receipt)) ?? receipt)
}

export function orderState(order: CurrentOrder, receipt?: CancelReceipt): string {
  if (receipt?.state === 'terminal' && receipt.order) return receipt.order.status
  if (receipt?.state === 'unknown' || order.uncertain) return '状态待确认'
  if (receipt?.state === 'submitting') return '正在提交撤单'
  if (receipt?.state === 'submitted') return '撤单确认中'
  return order.status
}

export function canCancelOrder(order: CurrentOrder, receipt: CancelReceipt | undefined, stale: boolean): boolean {
  return !stale && !order.uncertain && order.remaining_volume > 0
    && ['待成交', '已报', '部分成交'].includes(order.status)
    && (!receipt || receipt.state === 'failed')
}
