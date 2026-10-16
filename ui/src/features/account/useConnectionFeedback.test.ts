import { expect, test } from 'bun:test'
import { connectionFeedback } from '@/features/account/useConnectionFeedback'
import type { AccountConnectionDetail } from '@/types/api'

const disconnected = { status: 'disconnected' } as AccountConnectionDetail
const ready = { status: 'ready' } as AccountConnectionDetail
const error = new Error('GM 启动失败，具体原因未确认')
const failure = { pending: null, error, action: 'reconnect' as const, failedFrame: disconnected }

test('断开快照也能呈现连接操作失败，保持原始错误', () => {
  expect(connectionFeedback(failure, disconnected, true)).toEqual({ error, label: '连接失败' })
  expect(connectionFeedback(failure, disconnected, false)).toEqual({ error: null, label: null })
})

test('连接恢复解除旧失败，断开失败不会被仍然就绪的快照误清', () => {
  expect(connectionFeedback(failure, ready, true)).toEqual({ error: null, label: null })
  expect(connectionFeedback({ ...failure, failedFrame: ready }, ready, true).error).toBe(error)
  expect(connectionFeedback({ ...failure, failedFrame: ready }, { ...ready }, true).error).toBe(error)
  expect(connectionFeedback({ ...failure, action: 'disconnect' }, ready, true).label).toBe('断开失败')
})
