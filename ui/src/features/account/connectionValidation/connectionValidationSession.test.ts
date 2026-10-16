import { describe, expect, test } from 'bun:test'
import { ConnectionValidationSession } from './connectionValidationSession'
import type { ConnectionValidationJob } from '@/lib/api/connectionValidation'

const job = (status: ConnectionValidationJob['status'] = 'running'): ConnectionValidationJob => ({
  validation_id: 'test-validation', run_id: 'test-validation', purpose: 'validation', channel: 'ctp', status,
  steps: [], created_at: '2026-10-13T14:00:00', finished_at: null, error: null,
})
const tick = () => new Promise((resolve) => setTimeout(resolve, 10))

describe('连接验证生命周期', () => {
  test('离开页面后创建成功的任务也会取消，不会更新旧表单', async () => {
    let resolve!: (value: ConnectionValidationJob) => void
    const cancelled: string[] = []
    const updates: ConnectionValidationJob[] = []
    const session = new ConnectionValidationSession((value) => updates.push(value), () => {}, {
      create: () => new Promise((done) => { resolve = done }),
      get: async () => job(),
      cancel: async (id) => { cancelled.push(id); return job('cancelled') },
    })
    const started = session.start('ctp', {})
    session.dispose()
    resolve(job())
    await started
    expect(cancelled).toEqual(['test-validation'])
    expect(updates).toEqual([])
  })

  test('完成后停止轮询，卸载不会取消已完成任务', async () => {
    let reads = 0
    let cancels = 0
    const updates: string[] = []
    const session = new ConnectionValidationSession((value) => updates.push(value.status), () => {}, {
      create: async () => job(),
      get: async () => { reads++; return job('succeeded') },
      cancel: async () => { cancels++; return job('cancelled') },
    }, 1)
    await session.start('ctp', {})
    await tick()
    await tick()
    session.dispose()
    expect(reads).toBe(1)
    expect(cancels).toBe(0)
    expect(updates).toEqual(['running', 'succeeded'])
  })

  test('轮询失败保留任务，重新读取不重复创建验证', async () => {
    let cancels = 0
    const errors: string[] = []
    let reads = 0
    let creates = 0
    const updates: string[] = []
    const session = new ConnectionValidationSession(value => updates.push(value.status), (error) => errors.push(error.message), {
      create: async () => { creates++; return job() },
      get: async () => { if (++reads === 1) throw new Error('网络暂时不可用'); return job('succeeded') },
      cancel: async () => { cancels++; return job('cancelled') },
    }, 1)
    await session.start('ctp', {})
    await tick()
    expect(errors).toEqual(['网络暂时不可用'])
    expect(cancels).toBe(0)
    await session.refresh()
    expect(creates).toBe(1)
    expect(reads).toBe(2)
    expect(updates).toEqual(['running', 'succeeded'])
    session.dispose()
  })
})
