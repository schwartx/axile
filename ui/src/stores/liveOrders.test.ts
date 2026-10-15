import { beforeEach, expect, test } from 'bun:test'
import type { CurrentOrdersSnapshot } from '@/lib/api/orders'
import { useLiveOrders } from './liveOrders'

const snapshot = (version = 1, instance_id: string | null = 'worker'): CurrentOrdersSnapshot => ({
  data: [], count: 0, complete: true, sampled_at: '2026-10-15T10:00:00', cancellations: [], version, instance_id,
})
const apply = (version: number, value = snapshot(), kind: 'snapshot' | 'update' = 'snapshot', stream_id = 'stream') =>
  useLiveOrders.getState().apply({ stream_id, version, kind, data: [{ account_id: 1, snapshot: value }] })

beforeEach(() => useLiveOrders.setState({ frames: {}, streamId: null, version: -1, connected: false }))

test('manual reconcile cannot make a disconnected stream healthy or replace another worker', () => {
  apply(1)
  useLiveOrders.getState().disconnect()
  useLiveOrders.getState().applyRead(1, snapshot(3))
  expect(useLiveOrders.getState().connected).toBe(false)
  useLiveOrders.getState().applyRead(1, snapshot(99, 'old-worker'))
  expect(useLiveOrders.getState().frames[1].version).toBe(3)
})

test('SSE assembled before a manual response cannot roll back newer order progress', () => {
  apply(1)
  useLiveOrders.getState().applyRead(1, snapshot(3))
  apply(2, snapshot(2), 'update')
  expect(useLiveOrders.getState().frames[1].version).toBe(3)
  apply(3, { ...snapshot(3), complete: false, error: '连接中断' }, 'update')
  expect(useLiveOrders.getState().frames[1].error).toBe('连接中断')
})

test('server restart replaces old workers, and updates after disconnect wait for a full snapshot', () => {
  apply(10, snapshot(10, 'old'))
  useLiveOrders.getState().disconnect()
  apply(11, snapshot(11, 'old'), 'update')
  expect(useLiveOrders.getState().frames[1].version).toBe(10)
  apply(0, snapshot(0, 'new'), 'snapshot', 'restarted')
  expect(useLiveOrders.getState().frames[1].instance_id).toBe('new')
  expect(useLiveOrders.getState().connected).toBe(true)
})

test('initial unknown snapshot accepts explicit baseline and account deletion clears its data', () => {
  apply(1, { ...snapshot(0, null), complete: false, count: null })
  useLiveOrders.getState().applyRead(1, snapshot(1))
  expect(useLiveOrders.getState().frames[1].complete).toBe(true)
  useLiveOrders.getState().apply({ stream_id: 'stream', version: 2, kind: 'update', data: [], removed: [1] })
  expect(useLiveOrders.getState().frames[1]).toBeUndefined()
})
