import { useCallback, useRef, useState } from 'react'
import { usePolling } from '@/lib/hooks/usePolling'
import { cancelCurrentOrder, getCurrentOrders, type CancelReceipt, type CurrentOrder } from '@/lib/api/orders'
import { mergeReceipts } from '@/features/account/currentOrdersModel'

export function useCurrentOrders(accountId: number, inspectingHolding = false) {
  const [openFor, setOpenFor] = useState<number | null>(null)
  const [local, setLocal] = useState<{ accountId: number; values: CancelReceipt[] }>({ accountId, values: [] })
  const currentAccount = useRef(accountId)
  currentAccount.current = accountId
  const pending = useRef(new Set<string>())
  const force = useRef(false)
  const open = openFor === accountId
  const fetcher = useCallback((signal: AbortSignal) => {
    const refresh = force.current
    force.current = false
    return getCurrentOrders(accountId, signal, refresh)
  }, [accountId])
  const query = usePolling(fetcher, { queryKey: `current-orders:${accountId}`, intervalMs: open || inspectingHolding ? 3000 : 15000 })
  const receipts = mergeReceipts(query.data?.cancellations ?? [], local.accountId === accountId ? local.values : [])
  const read = query.refresh
  const refresh = useCallback(() => { force.current = true; return read() }, [read])
  const cancel = async (order: CurrentOrder) => {
    const key = `${accountId}:${order.order_id}`
    if (pending.current.has(key)) return
    pending.current.add(key)
    const requestId = crypto.randomUUID()
    const receipt: CancelReceipt = { request_id: requestId, order_id: order.order_id, symbol: order.symbol,
      state: 'submitting', message: '正在提交撤单请求', requested_at: new Date().toISOString(), order }
    const save = (value: CancelReceipt) => {
      if (currentAccount.current !== accountId) return
      setLocal(previous => ({ accountId, values: [...(previous.accountId === accountId ? previous.values : []).filter(r => r.order_id !== order.order_id), value] }))
    }
    save(receipt)
    try {
      save(await cancelCurrentOrder(accountId, order, requestId))
    } catch {
      // HTTP 失败也可能发生在柜台收到请求之后；保留未知状态并只重读，不重发撤单。
      save({ ...receipt, state: 'unknown', message: '撤单结果待确认，请刷新核对；未自动重试' })
    } finally {
      pending.current.delete(key)
      if (currentAccount.current === accountId) void refresh()
    }
  }
  return { query, open, show: () => setOpenFor(accountId), close: () => setOpenFor(null), refresh, cancel, receipts }
}

export type OrdersState = ReturnType<typeof useCurrentOrders>

