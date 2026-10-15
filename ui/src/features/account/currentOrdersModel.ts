import type { CancelReceipt, CurrentOrder, CurrentOrdersSnapshot } from '@/lib/api/orders'

export function orderOffset(offset: string | null): string {
  return ({ '0': '开仓', '1': '平仓', '3': '平今', '4': '平昨', OPEN: '开仓', CLOSE: '平仓', CLOSETODAY: '平今' } as Record<string, string>)[offset ?? ''] ?? '开平未知'
}

export function orderDirection(direction: string): string {
  return direction === 'BUY' || direction === '买入' ? '买入' : direction === 'SELL' || direction === '卖出' ? '卖出' : '方向未知'
}

export function orderSummary(snapshot: CurrentOrdersSnapshot | null, stale: boolean): string {
  if (!snapshot || !snapshot.complete || snapshot.count == null || stale) return '当前委托 待确认'
  return `当前委托 ${snapshot.count} 笔`
}

export function mergeReceipts(server: CancelReceipt[], local: CancelReceipt[]): CancelReceipt[] {
  const merged = new Map<string, CancelReceipt>()
  for (const receipt of [...local, ...server]) {
    const key = `${receipt.symbol}:${receipt.order_id}`
    const previous = merged.get(key)
    if (previous?.request_id === receipt.request_id) { merged.set(key, receipt); continue }
    if (previous && previous.request_id !== receipt.request_id && ['submitting', 'unknown'].includes(previous.state)) continue
    // 同一请求以服务端查询后的终态为准；新请求不被旧回执覆盖。
    if (!previous || receipt.requested_at >= previous.requested_at) merged.set(key, receipt)
  }
  return [...merged.values()]
}

export function orderState(order: CurrentOrder, receipt?: CancelReceipt): string {
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
