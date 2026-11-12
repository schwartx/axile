import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { cancelCurrentOrder, getAccountReconciliation, getCurrentOrders, type AccountAssetRefresh, type CancelReceipt, type CurrentOrder } from '@/lib/api/orders'
import { currentOrderKey, mergeReceipts, reconcileLocalReceipts, updateLocalReceipt } from '@/features/account/currentOrdersModel'
import { useLiveOrders } from '@/stores/liveOrders'
import { useAccountConnection } from '@/stores/liveConnection'
import { usePolling } from '@/lib/hooks/usePolling'

export function useCurrentOrders(accountId: number, onAssetsUpdated?: () => void) {
  const currentAccount = useRef(accountId)
  currentAccount.current = accountId
  const assetsUpdated = useRef(onAssetsUpdated)
  assetsUpdated.current = onAssetsUpdated
  const generation = useRef(0)
  const [assetSnapshot, setAssetSnapshot] = useState({ accountId, id: 0 })
  const [assetResult, setAssetResult] = useState<{ accountId: number; result: AccountAssetRefresh | null; generation: number } | null>(null)
  const finishAssetRefresh = useCallback((result: AccountAssetRefresh | null | undefined, token: number) => {
    if (currentAccount.current !== accountId || generation.current !== token) return
    setAssetResult({ accountId, result: result ?? null, generation: token })
    if (result?.status === 'updated') assetsUpdated.current?.()
  }, [accountId])
  const beginAssetRefresh = useCallback(() => {
    const token = ++generation.current
    setAssetResult({ accountId, result: { status: 'pending', message: '正在核对持仓' }, generation: token })
    return (result: AccountAssetRefresh | null | undefined) => finishAssetRefresh(result, token)
  }, [accountId, finishAssetRefresh])
  useEffect(() => () => { generation.current++ }, [accountId])
  const observeAssetSnapshot = useCallback((id: number) => {
    if (currentAccount.current !== accountId) return
    setAssetSnapshot(previous => previous.accountId === accountId && previous.id === id ? previous : { accountId, id })
  }, [accountId])
  const result = assetResult?.accountId === accountId ? assetResult.result : null
  const identity = result?.reconciliation_id ?? ''
  const token = assetResult?.generation ?? 0
  const reconciliation = usePolling(useCallback(async (signal: AbortSignal) => {
    const result = await getAccountReconciliation(accountId, identity, signal)
    if (!signal.aborted) finishAssetRefresh(result, token)
    return result
  }, [accountId, identity, token, finishAssetRefresh]), {
    queryKey: `asset-reconciliation:${accountId}:${identity}:${token}`,
    enabled: result?.status === 'pending' && !!identity, intervalMs: 5000,
  })
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
    const finish = beginAssetRefresh()
    try {
      const result = await getCurrentOrders(accountId, undefined, true)
      useLiveOrders.getState().applyRead(accountId, result)
      finish(result.asset_refresh)
    }
    catch (error) {
      setFailure({ accountId, error: error instanceof Error ? error : new Error(String(error)) })
      finish({ status: 'failed', message: '本次持仓核对结果待确认' })
    }
    finally { setChecking(current => current === accountId ? null : current) }
  }, [accountId, connectionReady, beginAssetRefresh])
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
  const awaitingSnapshot = result?.status === 'updated' && result.snapshot_id != null
    && (assetSnapshot.accountId !== accountId || assetSnapshot.id < result.snapshot_id)
  return { accountId, connectionReady, query, open, show, close: () => setOpenFor(null), refresh, cancel, receipts,
    assetRefresh: awaitingSnapshot ? { status: 'pending' as const, message: '正在更新持仓显示' }
      : result?.status === 'pending' && reconciliation.error ? { ...result, message: '核对结果暂不可用，持仓更新尚未确认' } : result,
    beginAssetRefresh, observeAssetSnapshot }
}

export type OrdersState = ReturnType<typeof useCurrentOrders>

