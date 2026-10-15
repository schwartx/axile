import { useCallback, useEffect, useRef, useState } from 'react'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

export interface PollingOptions {
  /** 查询身份。身份变化时旧数据立即失效，避免跨实体闪现。 */
  queryKey: string
  /** 轮询间隔；小于等于 0 时只取一次。 */
  intervalMs?: number
  /** 依赖未就绪时关闭查询。 */
  enabled?: boolean
  /** 仅由 refresh 发起读取，关闭首次加载、定时轮询及连接恢复读取。 */
  manual?: boolean
}

/** 轮询状态机。后台刷新保留上一次成功值。 */
export interface PollingState<T> {
  data: T | null
  error: Error | null
  /** 当前查询首次加载中（尚无任何成功数据）。 */
  loading: boolean
  /** 已有数据时的后台刷新；UI 默认静默。 */
  refreshing: boolean
  /** 最近一次成功的时间戳（ms）。 */
  updatedAt: number | null
  /** 已有成功数据、但最近一次刷新失败。 */
  stale: boolean
  /** 手动触发并等待一次真实刷新。 */
  refresh: () => Promise<void>
}

export interface StoredPollingState<T> {
  key: string | null
  data: T | null
  error: Error | null
  loading: boolean
  refreshing: boolean
  updatedAt: number | null
}

export interface PollingView<T> {
  data: T | null
  error: Error | null
  loading: boolean
  refreshing: boolean
  updatedAt: number | null
  stale: boolean
}

/** 按当前查询身份裁剪内部状态；key 变化后的首帧也绝不泄露旧数据。 */
export function pollingView<T>(
  state: StoredPollingState<T>,
  queryKey: string,
  enabled: boolean,
  manual = false,
): PollingView<T> {
  if (!enabled || state.key !== queryKey) {
    return {
      data: null,
      error: null,
      loading: enabled && !manual,
      refreshing: false,
      updatedAt: null,
      stale: false,
    }
  }
  return {
    data: state.data,
    error: state.error,
    loading: state.loading,
    refreshing: state.refreshing,
    updatedAt: state.updatedAt,
    stale: state.data !== null && state.error !== null,
  }
}

/**
 * 轮询一个异步取数函数。
 *
 * 同一 ``queryKey`` 刷新时保留成功数据；身份变化时立即隐藏旧数据。调用方必须用
 * ``useCallback`` 固定 fetcher，并把会改变响应身份的依赖编码进 ``queryKey``。
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  { queryKey, intervalMs = 5000, enabled = true, manual = false }: PollingOptions,
): PollingState<T> {
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const [state, setState] = useState<StoredPollingState<T>>({
    key: null,
    data: null,
    error: null,
    loading: false,
    refreshing: false,
    updatedAt: null,
  })
  const requestRef = useRef<{
    key: string
    id: number
    controller: AbortController
  } | null>(null)
  const requestIdRef = useRef(0)

  const run = useCallback((): Promise<void> => {
    if (!enabled) return Promise.resolve()

    requestRef.current?.controller.abort()
    const controller = new AbortController()
    const id = ++requestIdRef.current

    setState((previous) => {
      const sameKey = previous.key === queryKey
      const data = sameKey ? previous.data : null
      return {
        key: queryKey,
        data,
        error: sameKey ? previous.error : null,
        loading: data === null && (!sameKey || previous.error === null),
        refreshing: data !== null || (sameKey && previous.error !== null),
        updatedAt: sameKey ? previous.updatedAt : null,
      }
    })

    const promise = (async () => {
      try {
        const result = await fetcherRef.current(controller.signal)
        if (controller.signal.aborted || requestIdRef.current !== id) return
        setState({
          key: queryKey,
          data: result,
          error: null,
          loading: false,
          refreshing: false,
          updatedAt: Date.now(),
        })
      } catch (error) {
        if (controller.signal.aborted || requestIdRef.current !== id) return
        setState((previous) => ({
          key: queryKey,
          data: previous.key === queryKey ? previous.data : null,
          error: error instanceof Error ? error : new Error(String(error)),
          loading: false,
          refreshing: false,
          updatedAt: previous.key === queryKey ? previous.updatedAt : null,
        }))
      } finally {
        if (requestRef.current?.id === id) requestRef.current = null
      }
    })()

    requestRef.current = { key: queryKey, id, controller }
    return promise
  }, [enabled, queryKey])

  useEffect(() => {
    if (!enabled) {
      requestRef.current?.controller.abort()
      requestRef.current = null
      return
    }

    if (!manual) void run()
    const timer = !manual && intervalMs > 0
      ? window.setInterval(() => {
          if (!requestRef.current) void run()
        }, intervalMs)
      : undefined

    return () => {
      if (timer !== undefined) window.clearInterval(timer)
      if (requestRef.current?.key === queryKey) {
        requestRef.current.controller.abort()
        requestRef.current = null
        // 切回手动时，被取消的读取不能让刷新入口一直停在忙碌态。
        setState(previous => previous.key === queryKey ? { ...previous, loading: false, refreshing: false } : previous)
      }
    }
  }, [enabled, intervalMs, manual, queryKey, run])

  useReadRecovery(run, enabled && !manual)
  const view = pollingView(state, queryKey, enabled, manual)
  return { ...view, refresh: run }
}
