import { useCallback, useEffect, useRef, useState } from 'react'
import { usePolling } from '@/lib/hooks/usePolling'
import { cancelCurrentOrder, getCurrentOrders, type CancelReceipt, type CurrentOrder } from '@/lib/api/orders'
import { currentOrderKey, mergeReceipts, reconcileLocalReceipts, updateLocalReceipt } from '@/features/account/currentOrdersModel'
import { readOrderFetchMode, writeOrderFetchMode, type OrderFetchMode } from '@/features/account/currentOrdersPreferences'
import { useAccountConnection } from '@/stores/liveConnection'

export function useCurrentOrders(accountId: number, inspectingHolding = false) {
  const connectionReady = useAccountConnection(accountId).status === 'ready'
  const [openFor, setOpenFor] = useState<number | null>(null)
  const [preference, setPreference] = useState(() => ({ accountId, mode: readOrderFetchMode(accountId) }))
  const [local, setLocal] = useState<{ accountId: number; values: CancelReceipt[] }>({ accountId, values: [] })
  const fetchMode = preference.accountId === accountId ? preference.mode : readOrderFetchMode(accountId)
  const setFetchMode = (mode: OrderFetchMode) => {
    writeOrderFetchMode(accountId, mode)
    setPreference({ accountId, mode })
  }
  const currentAccount = useRef(accountId)
  currentAccount.current = accountId
  const pending = useRef(new Set<string>())
  const force = useRef(false)
  const open = openFor === accountId
  const viewing = open || inspectingHolding
  const currentView = useRef(viewing)
  currentView.current = viewing
  const fetcher = useCallback((signal: AbortSignal) => {
    const refresh = force.current
    force.current = false
    return getCurrentOrders(accountId, signal, refresh)
  }, [accountId])
  const polling = usePolling(fetcher, { queryKey: `current-orders:${accountId}`, enabled: connectionReady,
    intervalMs: fetchMode === 'manual' ? 0 : viewing ? 3000 : 15000, manual: fetchMode === 'manual' })
  const last = useRef<{ accountId: number; data: typeof polling.data; updatedAt: number | null } | null>(null)
  if (polling.data) last.current = { accountId, data: polling.data, updatedAt: polling.updatedAt }
  const retained = last.current?.accountId === accountId ? last.current : null
  const query = connectionReady ? polling : { ...polling, data: retained?.data ?? null,
    updatedAt: retained?.updatedAt ?? null, stale: retained?.data != null }
  const snapshot = query.data
  useEffect(() => {
    if (!snapshot?.cancellations.length) return
    setLocal(previous => {
      if (previous.accountId !== accountId || !previous.values.length) return previous
      const values = reconcileLocalReceipts(snapshot.cancellations, previous.values)
      return values.every((value, index) => value === previous.values[index]) ? previous : { accountId, values }
    })
  }, [accountId, snapshot])
  const receipts = mergeReceipts(snapshot?.cancellations ?? [], local.accountId === accountId ? local.values : [])
  const read = query.refresh
  const refresh = useCallback(() => { force.current = true; return read() }, [read])
  useEffect(() => {
    if (connectionReady && viewing && fetchMode === 'manual') void refresh()
  }, [connectionReady, viewing, fetchMode, refresh])
  const show = () => {
    setOpenFor(accountId)
  }
  const cancel = async (order: CurrentOrder) => {
    if (!connectionReady) return
    const key = `${accountId}:${currentOrderKey(order)}`
    if (pending.current.has(key)) return
    pending.current.add(key)
    const requestId = crypto.randomUUID()
    const receipt: CancelReceipt = { request_id: requestId, order_id: order.order_id, symbol: order.symbol,
      state: 'submitting', message: '正在提交撤单请求', requested_at: new Date().toISOString(), order }
    const save = (value: CancelReceipt) => {
      if (currentAccount.current !== accountId) return
      setLocal(previous => ({ accountId, values: updateLocalReceipt(previous.accountId === accountId ? previous.values : [], value) }))
    }
    save(receipt)
    try {
      save(await cancelCurrentOrder(accountId, order, requestId))
    } catch {
      // HTTP 失败也可能发生在柜台收到请求之后；保留未知状态并只重读，不重发撤单。
      save({ ...receipt, state: 'unknown', message: '撤单结果待确认，请刷新核对；未自动重试' })
    } finally {
      pending.current.delete(key)
      if (currentAccount.current === accountId && currentView.current) void refresh()
    }
  }
  return { accountId, connectionReady, query, open, show, close: () => setOpenFor(null), refresh, cancel, receipts, fetchMode, setFetchMode }
}

export type OrdersState = ReturnType<typeof useCurrentOrders>

