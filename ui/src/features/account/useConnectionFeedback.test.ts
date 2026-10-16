import { expect, test } from 'bun:test'
import { connectionFeedback } from '@/features/account/useConnectionFeedback'
import type { AccountConnectionDetail } from '@/types/api'
import { apiErrorFromBody } from '@/lib/api/client'

const disconnected = { status: 'disconnected' } as AccountConnectionDetail
const ready = { status: 'ready' } as AccountConnectionDetail
const error = new Error('GM 启动失败，具体原因未确认')
const failure = { pending: null, error, action: 'reconnect' as const, failedFrame: disconnected }

test('断开快照也能呈现连接操作失败，保持原始错误', () => {
  expect(connectionFeedback(failure, disconnected, true)).toEqual({ error, label: error.message })
  expect(connectionFeedback(failure, disconnected, false)).toEqual({ error: null, label: null })
})

test('连接恢复解除旧失败，断开失败不会被仍然就绪的快照误清', () => {
  expect(connectionFeedback(failure, ready, true)).toEqual({ error: null, label: null })
  expect(connectionFeedback({ ...failure, failedFrame: ready }, ready, true).error).toBe(error)
  expect(connectionFeedback({ ...failure, failedFrame: ready }, { ...ready }, true).error).toBe(error)
  expect(connectionFeedback({ ...failure, action: 'disconnect' }, ready, true).label).toBe(error.message)
})

test('连接标识保留 API 的具体断开原因和完整长文案', () => {
  const error = apiErrorFromBody(503, 'Service Unavailable', { detail: { message: 'CTP 交易前置已断开' } })
  expect(connectionFeedback({ ...failure, error }, disconnected, true).label).toBe('CTP 交易前置已断开')
  const long = '连接失败原因'.repeat(50)
  expect(connectionFeedback({ ...failure, error: new Error(long) }, disconnected, true).label).toBe(long)
})

test('缺失原因时保留连接或断开操作的兜底文案', () => {
  const empty = { ...failure, error: new Error('   ') }
  expect(connectionFeedback(empty, disconnected, true).label).toBe('连接失败')
  expect(connectionFeedback({ ...empty, action: 'disconnect' }, ready, true).label).toBe('断开失败')
})
