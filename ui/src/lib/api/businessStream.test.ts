import { useLiveOrders } from '@/stores/liveOrders'
import { afterEach, beforeEach, expect, test } from 'bun:test'
import { useLiveConnection } from '@/stores/liveConnection'
import { BUSINESS_STREAM_PATH, startBusinessStream } from './businessStream'
import { useLiveExecStore, type RunFrame } from '@/stores/liveExec'
import { useLiveControl, type ControlEnvelope } from '@/stores/liveControl'

class Source extends EventTarget {
  closed = false
  onerror: (() => void) | null = null
  close() { this.closed = true }
  send(name: string, data: unknown) { this.dispatchEvent(new MessageEvent(name, { data: JSON.stringify(data) })) }
}
class Visibility extends EventTarget {
  hidden = false
  change(hidden: boolean) { this.hidden = hidden; this.dispatchEvent(new Event('visibilitychange')) }
}
const run = (execution_id = 'run', status = 'running'): RunFrame => ({ account_id: 1, execution_id, kind: 'rebalance', phase: 'executing', status })
const snapshot = (version = 1, data = [run()], stream_id = 'exec') => ({ stream_id, version, data })
const update = (version: number, frame = run(), stream_id = 'exec') => ({ ...frame, stream_id, version })
const control = (version = 1, kind: 'snapshot' | 'update' = 'snapshot', stream_id = 'control'): ControlEnvelope => ({ stream_id, version, kind, data: [] })

let visibility: Visibility
let opened: Source[]
let paths: string[]
let recurring: Set<() => void>
let deferred: Set<() => void>
let stop: (() => void) | undefined
function start() {
  stop = startBusinessStream({
    visibility,
    open: (path) => { const source = new Source(); opened.push(source); paths.push(path); return source as unknown as EventSource },
    every: (callback) => { recurring.add(callback); return () => { recurring.delete(callback) } },
    later: (callback) => { deferred.add(callback); return () => { deferred.delete(callback) } },
  })
}
beforeEach(() => {
  visibility = new Visibility(); opened = []; paths = []; recurring = new Set(); deferred = new Set()
  useLiveExecStore.setState({ running: new Map(), connected: false, error: null })
  useLiveControl.setState({ frames: {}, streamId: null, version: -1, connected: false, error: null, lastReceivedAt: null, disconnectedAt: null })
  useLiveConnection.setState({ frames: {}, streamId: null, version: -1, connected: false })
  useLiveOrders.setState({ frames: {}, streamId: null, version: -1, connected: false })
  start()
})
afterEach(() => { stop?.(); stop = undefined })

test('one connection dispatches both domains and snapshots replace only their own state', () => {
  expect(paths).toEqual([BUSINESS_STREAM_PATH])
  opened[0].send('execution.snapshot', snapshot())
  opened[0].send('control.snapshot', control())
  expect(useLiveExecStore.getState().running.get(1)?.executionId).toBe('run')
  expect(useLiveExecStore.getState().connected).toBe(true)
  expect(useLiveControl.getState().connected).toBe(true)
  opened[0].send('control.snapshot', control(2))
  expect(useLiveExecStore.getState().running.size).toBe(1)
  opened[0].send('execution.snapshot', snapshot(2, []))
  expect(useLiveControl.getState().version).toBe(2)
  opened[0].send('future.update', { anything: true })
  expect(opened.length).toBe(1)
})

test('execution requires a snapshot and rejects duplicate, older and foreign-stream messages', () => {
  opened[0].send('execution.event', update(9))
  expect(useLiveExecStore.getState().running.size).toBe(0)
  opened[0].send('execution.snapshot', snapshot(10))
  opened[0].send('execution.event', update(11, { ...run(), phase: 'settling' }))
  opened[0].send('execution.snapshot', snapshot(10, []))
  opened[0].send('execution.event', update(11, run('duplicate')))
  opened[0].send('execution.event', update(12, run('foreign'), 'other'))
  expect(useLiveExecStore.getState().running.get(1)?.phase).toBe('settling')
  expect(useLiveExecStore.getState().running.get(1)?.executionId).toBe('run')
})

test('late terminal for a previous ticket cannot clear the next ticket', () => {
  opened[0].send('execution.snapshot', snapshot(1, [run('next')]))
  opened[0].send('execution.event', update(2, run('previous', 'done')))
  expect(useLiveExecStore.getState().running.get(1)?.executionId).toBe('next')
  opened[0].send('execution.event', update(3, run('next', 'done')))
  expect(useLiveExecStore.getState().running.size).toBe(0)
})

test('control errors keep execution ready and recovery requires a control snapshot', () => {
  opened[0].send('execution.snapshot', snapshot())
  opened[0].send('control.snapshot', control())
  opened[0].send('control.error', { message: 'sampler unavailable' })
  expect(useLiveExecStore.getState().connected).toBe(true)
  expect(useLiveControl.getState().connected).toBe(false)
  expect(useLiveControl.getState().error).toBe('sampler unavailable')
  opened[0].send('control.update', control(2, 'update'))
  expect(useLiveControl.getState().version).toBe(1)
  opened[0].send('control.snapshot', control(3))
  expect(useLiveControl.getState().connected).toBe(true)
  expect(useLiveControl.getState().error).toBeNull()
  opened[0].send('execution.error', { message: 'execution unavailable' })
  expect(useLiveControl.getState().connected).toBe(true)
  expect(useLiveExecStore.getState().error).toBe('execution unavailable')
})

test('visibility changes close and reopen once, with old connection events ignored', () => {
  opened[0].send('execution.snapshot', snapshot())
  visibility.change(true)
  expect(opened[0].closed).toBe(true)
  expect(useLiveExecStore.getState().connected).toBe(false)
  expect(useLiveExecStore.getState().running.size).toBe(1)
  visibility.change(false)
  visibility.change(false)
  expect(opened.length).toBe(2)
  opened[1].send('execution.snapshot', snapshot(2, [run('next')]))
  opened[0].send('execution.snapshot', snapshot(99, []))
  opened[0].onerror?.()
  expect(useLiveExecStore.getState().running.get(1)?.executionId).toBe('next')
  expect(useLiveExecStore.getState().connected).toBe(true)
})

test('native reconnect retains last data and waits for independent new snapshots', () => {
  opened[0].send('execution.snapshot', snapshot(9))
  opened[0].send('control.snapshot', control(9))
  opened[0].onerror?.()
  opened[0].send('execution.event', update(10, run('premature')))
  opened[0].send('control.update', control(10, 'update'))
  expect(useLiveExecStore.getState().running.get(1)?.executionId).toBe('run')
  expect(useLiveControl.getState().version).toBe(9)
  opened[0].send('execution.snapshot', snapshot(0, [run('restart')], 'restarted-exec'))
  expect(useLiveExecStore.getState().connected).toBe(true)
  expect(useLiveControl.getState().connected).toBe(false)
  opened[0].send('control.snapshot', control(0, 'snapshot', 'restarted-control'))
  expect(useLiveControl.getState().connected).toBe(true)
  expect(opened.length).toBe(1)
})

test('malformed payload reopens with a snapshot and cancels retry while hidden', () => {
  opened[0].dispatchEvent(new MessageEvent('control.snapshot', { data: '{invalid' }))
  expect(opened[0].closed).toBe(true)
  expect(deferred.size).toBe(1)
  visibility.change(true)
  expect(deferred.size).toBe(0)
  visibility.change(false)
  expect(opened.length).toBe(2)
  opened[1].send('execution.snapshot', snapshot())
  expect(useLiveExecStore.getState().connected).toBe(true)
})

test('deferred recovery creates one new connection and clears timers on cleanup', () => {
  opened[0].send('execution.snapshot', { stream_id: 'exec', version: -1, data: [] })
  for (const callback of [...deferred]) { deferred.delete(callback); callback() }
  expect(opened.length).toBe(2)
  expect(recurring.size).toBe(1)
  stop?.()
  expect(recurring.size).toBe(0)
  expect(deferred.size).toBe(0)
  expect(opened.every(source => source.closed)).toBe(true)
  visibility.change(false)
  expect(opened.length).toBe(2)
})

test('mount cleanup and remount keep one active connection and expiry timer', () => {
  stop?.()
  start()
  expect(opened.filter(source => !source.closed).length).toBe(1)
  expect(recurring.size).toBe(1)
  opened[0].send('control.snapshot', control(99))
  expect(useLiveControl.getState().connected).toBe(false)
  opened[1].send('control.snapshot', control())
  expect(useLiveControl.getState().connected).toBe(true)
})

test('connection domain waits for its snapshot and never uses recurring reconciliation', () => {
  const frame = { account_id: 7, connection: { status: 'ready', observed_at: 'now', message: null, error: null } }
  opened[0].send('connection.update', { stream_id: 'conn', version: 2, kind: 'update', data: [frame] })
  expect(useLiveConnection.getState().connected).toBe(false)
  opened[0].send('connection.snapshot', { stream_id: 'conn', version: 1, kind: 'snapshot', data: [frame] })
  expect(useLiveConnection.getState().frames[7].status).toBe('ready')
  opened[0].send('connection.update', { stream_id: 'conn', version: 2, kind: 'update', data: [{ ...frame, connection: { ...frame.connection, status: 'unavailable' } }] })
  opened[0].send('connection.update', { stream_id: 'conn', version: 1, kind: 'update', data: [frame] })
  for (let i = 0; i < 60; i++) for (const callback of recurring) callback()
  expect(useLiveConnection.getState().frames[7].status).toBe('unavailable')
  expect(useLiveConnection.getState().version).toBe(2)
  expect(opened.length).toBe(1)
})

test('connection loss clears readiness and equal-version reconnect snapshot restores it', () => {
  const message = { stream_id: 'conn', version: 3, kind: 'snapshot', data: [{ account_id: 7,
    connection: { status: 'ready', observed_at: 'now', message: null, error: null } }] }
  opened[0].send('connection.snapshot', message)
  opened[0].onerror?.()
  expect(useLiveConnection.getState().connected).toBe(false)
  opened[0].send('connection.update', { ...message, version: 4, kind: 'update' })
  expect(useLiveConnection.getState().connected).toBe(false)
  expect(useLiveConnection.getState().version).toBe(3)
  opened[0].send('connection.snapshot', message)
  expect(useLiveConnection.getState().connected).toBe(true)
  opened[0].send('connection.update', { ...message, version: 4, kind: 'update', data: [], removed: [7] })
  expect(useLiveConnection.getState().frames[7]).toBeUndefined()
})

test('connection errors stay isolated and malformed states require a fresh stream', () => {
  opened[0].send('execution.snapshot', snapshot())
  opened[0].send('control.snapshot', control())
  opened[0].send('connection.snapshot', { stream_id: 'conn', version: 1, kind: 'snapshot', data: [] })
  opened[0].send('connection.error', { message: 'connection snapshot unavailable' })
  expect(useLiveExecStore.getState().connected).toBe(true)
  expect(useLiveControl.getState().connected).toBe(true)
  expect(useLiveConnection.getState().connected).toBe(false)
  opened[0].send('connection.snapshot', { stream_id: 'restarted', version: 0, kind: 'snapshot', data: [] })
  expect(useLiveConnection.getState().connected).toBe(true)
  opened[0].send('connection.update', { stream_id: 'restarted', version: 1, kind: 'update', data: [{ account_id: 7, connection: { status: 'invalid' } }] })
  expect(opened[0].closed).toBe(true)
  expect(useLiveConnection.getState().connected).toBe(false)
  expect(deferred.size).toBe(1)
})


test('orders use the existing SSE and isolate errors and reconnect snapshots', () => {
  const frame = { account_id: 1, snapshot: { data: [], count: 0, complete: true, sampled_at: '2026-10-15T10:00:00', cancellations: [], version: 1, instance_id: 'worker' } }
  const envelope = { stream_id: 'orders', version: 1, kind: 'snapshot', data: [frame] }
  opened[0].send('orders.update', { ...envelope, kind: 'update' })
  expect(useLiveOrders.getState().connected).toBe(false)
  opened[0].send('orders.snapshot', envelope)
  expect(useLiveOrders.getState().frames[1].count).toBe(0)
  opened[0].send('execution.snapshot', snapshot())
  opened[0].send('orders.error', { message: 'orders unavailable' })
  expect(useLiveExecStore.getState().connected).toBe(true)
  expect(useLiveOrders.getState().connected).toBe(false)
  opened[0].send('orders.snapshot', envelope)
  expect(useLiveOrders.getState().connected).toBe(true)
  visibility.change(true)
  expect(useLiveOrders.getState().connected).toBe(false)
  expect(opened.length).toBe(1)
})

test('manual order response cannot overwrite newer SSE or another worker', () => {
  const value = { data: [], count: 0, complete: true, sampled_at: 'now', cancellations: [], version: 4, instance_id: 'new' }
  useLiveOrders.getState().apply({ stream_id: 'o', version: 1, kind: 'snapshot', data: [{ account_id: 1, snapshot: value }] })
  useLiveOrders.getState().applyRead(1, { ...value, version: 3, count: 9 })
  useLiveOrders.getState().applyRead(1, { ...value, instance_id: 'old', version: 99, count: 9 })
  expect(useLiveOrders.getState().frames[1].count).toBe(0)
})
