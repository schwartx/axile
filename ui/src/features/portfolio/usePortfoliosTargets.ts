import { useCallback, useEffect, useRef, useState } from 'react'
import { getPortfolioTargetSnapshot } from '@/lib/api/portfolios'
import type { TargetWeightSnapshot } from '@/types/api'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

export interface PortfolioTargetFetch {
  loading: boolean
  snapshot: TargetWeightSnapshot | null
  error: Error | null
  updatedAt: number | null
  refresh: () => Promise<void>
}
/** 每个来源保留完整错误、旧快照与独立重试；旧请求不覆盖新结果。 */
export function usePortfoliosTargets(portfolioIds: (number | null | undefined)[]): Record<number, PortfolioTargetFetch> {
  const key = portfolioIds.filter((id): id is number => id != null).join(',')
  const [states, setStates] = useState<Record<number, Omit<PortfolioTargetFetch, 'refresh'>>>({})
  const controllers = useRef(new Map<number, AbortController>())
  const load = useCallback(async (id: number) => {
    controllers.current.get(id)?.abort()
    const controller = new AbortController()
    controllers.current.set(id, controller)
    setStates(previous => ({ ...previous, [id]: { loading: true, snapshot: previous[id]?.snapshot ?? null, error: null, updatedAt: previous[id]?.updatedAt ?? null } }))
    try {
      const snapshot = await getPortfolioTargetSnapshot(id, controller.signal)
      if (!controller.signal.aborted) setStates(previous => ({ ...previous, [id]: { loading: false, snapshot, error: null, updatedAt: Date.now() } }))
    } catch (cause) {
      if (!controller.signal.aborted) setStates(previous => ({ ...previous, [id]: { ...previous[id]!, loading: false, error: cause instanceof Error ? cause : new Error(String(cause)) } }))
    }
  }, [])
  useEffect(() => {
    setStates(previous => Object.fromEntries(key.split(',').filter(Boolean).filter(id => previous[Number(id)]).map(id => [id, previous[Number(id)]])))
    for (const id of key.split(',').filter(Boolean).map(Number)) void load(id)
    const active = controllers.current
    return () => { for (const controller of active.values()) controller.abort(); active.clear() }
  }, [key, load])
  useReadRecovery(() => Promise.all([...new Set(key.split(',').filter(Boolean).map(Number))].map(load)))
  return Object.fromEntries(Object.entries(states).map(([id, value]) => [id, { ...value, refresh: () => load(Number(id)) }]))
}
