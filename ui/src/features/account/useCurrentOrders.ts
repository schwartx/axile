import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { cancelCurrentOrder, getCurrentOrders, type CancelReceipt, type CurrentOrder } from '@/lib/api/orders'
import { currentOrderKey, mergeReceipts, reconcileLocalReceipts, updateLocalReceipt } from '@/features/account/currentOrdersModel'
import { useLiveOrders } from '@/stores/liveOrders'
import { useAccountConnection } from '@/stores/liveConnection'

export function useCurrentOrders(accountId: number) {
  const connectionReady = useAccountConnection(accountId).status === 'ready'
  const [openFor, setOpenFor] = useState<number | null>(null)
  const [local, setLocal] = useState<{ accountId: number; values: CancelReceipt[] }>({ accountId, values: [] })
  const [checking, setChecking] = useState<number | null>(null)
  const [failure, setFailure] = useState<{ accountId: number; error: Error } | null>(null)
  const data = useLiveOrders(state => state.frames[accountId]) ?? null
  const connected = useLiveOrders(state => state.connected)
  const error = useMemo(() => data?.error ? new Error(data.error) : failure?.accountId === accountId ? failure.error : null, [accountId, data?.error, failure])
  const refresh = useCallback(async () => {
    if (!connectionReady) return
    setChecking(accountId)
    setFailure(null)
    try { useLiveOrders.getState().applyRead(accountId, await getCurrentOrders(accountId, undefined, true)) }
    catch (error) { setFailure({ accountId, error: error instanceof Error ? error : new Error(String(error)) }) }
    finally { setChecking(current => current === accountId ? null : current) }
  }, [accountId, connectionReady])
  const currentAccount = useRef(accountId)
  currentAccount.current = accountId
  const pending = useRef(new Set<string>())
  const open = openFor === accountId
  const query = { data, error, refreshError: failure?.accountId === accountId ? failure.error : null, stale: !connected || !connectionReady || !!error,
    loading: connectionReady && !data, refreshing: checking === accountId,
    updatedAt: data && (data.complete || data.data.length > 0 || data.count != null) ? Date.parse(data.sampled_at) : null, refresh }
  const snapshot = query.data
  useEffect(() => {
    if (snapshot && !snapshot.error) setFailure(null)
  }, [accountId, snapshot])
  useEffect(() => {
    if (!snapshot?.cancellations.length) return
    setLocal(previous => {
      if (previous.accountId !== accountId || !previous.values.length) return previous
      const values = reconcileLocalReceipts(snapshot.cancellations, previous.values)
      return values.every((value, index) => value === previous.values[index]) ? previous : { accountId, values }
    })
  }, [accountId, snapshot])
  const receipts = mergeReceipts(snapshot?.cancellations ?? [], local.accountId === accountId ? local.values : [])
  const show = () => {
    setOpenFor(accountId)
  }
  const cancel = async (order: CurrentOrder) => {
    if (query.stale || !snapshot?.complete) return
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
      // HTTP 失败也可能发生在柜台收到请求之后；保留未知状态，等待回报或人工核对。
      save({ ...receipt, state: 'unknown', message: '撤单结果待确认，请刷新核对；未自动重试' })
    } finally {
      pending.current.delete(key)
    }
  }
  return { accountId, connectionReady, query, open, show, close: () => setOpenFor(null), refresh, cancel, receipts }
}

export type OrdersState = ReturnType<typeof useCurrentOrders>

