import { useEffect } from 'react'
import { useDomainStore } from '@/stores/domain'
import { useLiveExecStore, type RunFrame } from '@/stores/liveExec'
import { useLiveControl, type ControlEnvelope } from '@/stores/liveControl'
import { useLiveConnection, type ConnectionEnvelope } from '@/stores/liveConnection'
import { useLiveOrders, type OrdersEnvelope } from '@/stores/liveOrders'

export const BUSINESS_STREAM_PATH = '/api/v1/events/stream'

interface StreamCursor { stream_id: string; version: number }
export interface ExecutionSnapshot extends StreamCursor { data: RunFrame[] }
export interface ExecutionUpdate extends StreamCursor, RunFrame {}
type StreamSource = Pick<EventSource, 'addEventListener' | 'close' | 'onerror'>
interface StreamOptions {
  visibility?: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>
  open?: (path: string) => StreamSource
  every?: (callback: () => void) => () => void
  later?: (callback: () => void) => () => void
}

function cursor(value: StreamCursor): StreamCursor {
  if (!value || typeof value.stream_id !== 'string' || !Number.isSafeInteger(value.version) || value.version < 0) {
    throw new Error('实时事件缺少有效的流会话或版本')
  }
  return value
}

/** 每次挂载拥有一条连接；原生重连与主动重连均要求各域先取得快照。 */
export function startBusinessStream({
  visibility = document,
  open = (path) => new EventSource(path),
  every = (callback) => { const timer = window.setInterval(callback, 1000); return () => window.clearInterval(timer) },
  later = (callback) => { const timer = window.setTimeout(callback, 1000); return () => window.clearTimeout(timer) },
}: StreamOptions = {}): () => void {
  let stream: StreamSource | null = null
  let disposed = false
  let cancelRetry: (() => void) | null = null
  const disconnect = () => {
    useLiveExecStore.getState().disconnect()
    useLiveControl.getState().disconnect()
    useLiveConnection.getState().disconnect()
    useLiveOrders.getState().disconnect()
  }
  const close = () => {
    const previous = stream
    stream = null
    previous?.close()
  }
  const sync = () => {
    cancelRetry?.()
    cancelRetry = null
    if (disposed) return
    if (visibility.hidden) { close(); disconnect(); return }
    if (stream) return
    useLiveExecStore.getState().beginReconnect()
    useLiveControl.getState().beginReconnect()
    useLiveConnection.getState().disconnect()
    useLiveOrders.getState().disconnect()
    const current = open(BUSINESS_STREAM_PATH)
    stream = current
    let executionCursor: StreamCursor | null = null
    let executionReady = false
    let controlReady = false
    let connectionReady = false
    let ordersReady = false

    const fail = (topic: 'execution' | 'control' | 'connection' | 'orders', message: string) => {
      if (topic === 'execution') { executionReady = false; useLiveExecStore.getState().disconnect(message) }
      else if (topic === 'control') { controlReady = false; useLiveControl.getState().disconnect(undefined, message) }
      else if (topic === 'orders') { ordersReady = false; useLiveOrders.getState().disconnect() }
      else { connectionReady = false; useLiveConnection.getState().disconnect() }
    }
    const listen = <T,>(name: string, receive: (data: T) => void) => {
      current.addEventListener(name, (event) => {
        if (disposed || stream !== current) return
        try { receive(JSON.parse((event as MessageEvent<string>).data) as T) }
        catch (error) {
          // 无法应用的帧可能已造成缺口，重开连接取得全量；旧连接从此失效。
          disconnect()
          fail(name.startsWith('execution.') ? 'execution' : name.startsWith('connection.') ? 'connection' : name.startsWith('orders.') ? 'orders' : 'control', error instanceof Error ? error.message : String(error))
          close()
          cancelRetry = later(sync)
        }
      })
    }
    listen<ExecutionSnapshot>('execution.snapshot', (message) => {
      const incoming = cursor(message)
      if (!Array.isArray(message.data)) throw new Error('执行快照缺少账户列表')
      if (executionCursor?.stream_id === incoming.stream_id && (incoming.version < executionCursor.version || (executionReady && incoming.version === executionCursor.version))) return
      useLiveExecStore.getState().applySnapshot(message.data)
      executionCursor = incoming
      executionReady = true
    })
    listen<ExecutionUpdate>('execution.event', (message) => {
      const incoming = cursor(message)
      if (!executionReady || incoming.stream_id !== executionCursor?.stream_id || incoming.version <= executionCursor.version) return
      useLiveExecStore.getState().applyEvent(message)
      executionCursor = incoming
    })
    const control = (message: ControlEnvelope, snapshot: boolean) => {
      cursor(message)
      if (!Array.isArray(message.data) || message.kind !== (snapshot ? 'snapshot' : 'update')) throw new Error('流控事件类型或账户列表无效')
      if (!snapshot && !controlReady) return
      useLiveControl.getState().apply(message)
      controlReady = useLiveControl.getState().connected
    }
    listen<ControlEnvelope>('control.snapshot', (message) => control(message, true))
    listen<ControlEnvelope>('control.update', (message) => control(message, false))
    const connection = (message: ConnectionEnvelope, snapshot: boolean) => {
      cursor(message)
      if (!Array.isArray(message.data) || message.kind !== (snapshot ? 'snapshot' : 'update')) throw new Error('连接状态帧无效')
      for (const frame of message.data) {
        if (!Number.isSafeInteger(frame.account_id) || !frame.connection
          || !['uninitialized', 'unknown', 'ready', 'unavailable', 'connecting', 'recovering', 'disconnected'].includes(frame.connection.status)) {
          throw new Error('连接状态缺少有效账户或会话状态')
        }
      }
      if (!snapshot && !connectionReady) return
      useLiveConnection.getState().apply(message)
      connectionReady = useLiveConnection.getState().connected
    }
    listen<ConnectionEnvelope>('connection.snapshot', (message) => connection(message, true))
    listen<ConnectionEnvelope>('connection.update', (message) => connection(message, false))
    const orders = (message: OrdersEnvelope, snapshot: boolean) => {
      cursor(message)
      if (!Array.isArray(message.data) || message.kind !== (snapshot ? 'snapshot' : 'update')) throw new Error('委托快照无效')
      for (const frame of message.data) {
        if (!Number.isSafeInteger(frame.account_id) || !frame.snapshot || !Array.isArray(frame.snapshot.data)
          || typeof frame.snapshot.complete !== 'boolean') throw new Error('账户委托数据无效')
      }
      if (!snapshot && !ordersReady) return
      useLiveOrders.getState().apply(message)
      ordersReady = useLiveOrders.getState().connected
    }
    listen<OrdersEnvelope>('orders.snapshot', message => orders(message, true))
    listen<OrdersEnvelope>('orders.update', message => orders(message, false))
    for (const topic of ['execution', 'control', 'connection', 'orders'] as const) {
      listen<{ message: string }>(`${topic}.error`, (message) => fail(topic, message.message))
    }
    current.onerror = () => {
      if (disposed || stream !== current) return
      executionReady = controlReady = connectionReady = ordersReady = false
      disconnect()
      // EventSource 自动重连；open 不表示业务已就绪，等待各域自己的快照。
    }
  }
  sync()
  visibility.addEventListener('visibilitychange', sync)
  const cancelExpiry = every(() => { if (!visibility.hidden) useLiveControl.getState().expire(Date.now()) })
  return () => {
    disposed = true
    visibility.removeEventListener('visibilitychange', sync)
    cancelRetry?.()
    cancelExpiry()
    close()
    disconnect()
  }
}

/** 应用根挂载一次；保留账户 REST 刷新驱动的执行态对账。 */
export function useBusinessSync(): void {
  useEffect(() => startBusinessStream(), [])
  const accounts = useDomainStore((state) => state.accounts)
  useEffect(() => { if (accounts) useLiveExecStore.getState().reconcile(accounts) }, [accounts])
}
