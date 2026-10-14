import { describe, expect, it } from 'bun:test'

import { ApiError } from '@/lib/api/client'
import { errorInfo, isConnectionError, shortErrorReason } from './errorInfo'

describe('errorInfo', () => {
  it('turns fetch failures into an actionable service message', () => {
    expect(shortErrorReason(new TypeError('Failed to fetch'))).toBe('无法连接 axile 服务')
  })

  it('distinguishes network failures from HTTP and business errors', () => {
    expect(isConnectionError(new TypeError('Failed to fetch'))).toBe(true)
    expect(isConnectionError(new TypeError('Load failed'))).toBe(true)
    expect(isConnectionError(new Error('Failed to fetch'))).toBe(false)
    expect(isConnectionError(new Error('HTTP 503'))).toBe(false)
  })

  it('keeps long raw text, newlines, stack and credentials outside the short summary', () => {
    const raw = 'x'.repeat(300) + '\npassword=hunter2 token=abc api_key=xyz\n尾部根因'
    const error = new Error(raw)
    error.stack = 'Traceback\n  line 12\n' + raw
    const info = errorInfo(error)
    expect(info.message.length).toBe(160)
    expect(info.details[0]?.value).toBe(raw)
    expect(info.text).toContain(error.stack)
    expect(info.text).toContain('尾部根因')
    expect(shortErrorReason(new Error('password=hunter2'))).toBe('password=hunter2')
  })

  it('exposes only safe ApiError evidence', () => {
    const info = errorInfo(new ApiError(409, '冲突', { code: 'RUNNING', requestId: 'req-2' }))
    expect(info.evidence).toEqual([
      { label: 'HTTP', value: '409' },
      { label: '错误码', value: 'RUNNING' },
      { label: '请求标识', value: 'req-2' },
    ])
  })
})

it('renders nested technical details and traceback with their actual newlines', () => {
  const trace = 'Traceback\n  actual script line\npassword=synthetic\n尾部根因'
  const error = new ApiError(500, 'request failed', { body: { detail: { technical_detail: 'raw\nreason', traceback: trace } } })
  const info = errorInfo(error)
  expect(info.details).toContainEqual({ label: 'detail · traceback', value: trace })
  expect(info.text).toContain(trace)
  expect(info.text).toContain('raw\nreason')
})
