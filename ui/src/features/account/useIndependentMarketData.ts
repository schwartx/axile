import { useCallback } from 'react'
import { getAccountMarketDataStatus, type MarketDataStatus } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'

export const independentMarketDataLabels: Record<MarketDataStatus, string> = {
  uninitialized: '未使用',
  connecting: '连接中',
  ready: '可用',
  unavailable: '暂不可用',
  disconnected: '已断开',
}

export function useIndependentMarketData(accountId: number, enabled: boolean) {
  const fetchStatus = useCallback((signal: AbortSignal) => getAccountMarketDataStatus(accountId, signal), [accountId])
  const { data, error } = usePolling(fetchStatus, { queryKey: `market-data-status:${accountId}`, enabled })
  return error ? null : data?.status ?? null
}
