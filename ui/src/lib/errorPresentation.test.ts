import { describe, expect, it } from 'bun:test'
import { ApiError } from '@/lib/api/client'
import { errorDisposition } from '@/lib/errorPresentation'

const network = new TypeError('Failed to fetch')
const online = { disconnected: false, account: null }
const offline = { disconnected: true, account: null }

describe('错误展示归属', () => {
  it('首次读取和缓存刷新都由已确认的全局失联入口承接', () => {
    expect(errorDisposition(network, {}, offline)).toBe('unavailable')
    expect(errorDisposition(network, { hasData: true }, offline)).toBe('stale')
    expect(errorDisposition(network, {}, online)).toBe('local')
  })

  it('独立 HTTP 错误不因全局失联而消失', () => {
    for (const status of [404, 422, 500, 503]) {
      expect(errorDisposition(new ApiError(status, '独立错误'), {}, offline)).toBe('local')
    }
    expect(errorDisposition(new Error('Failed to fetch'), {}, offline)).toBe('local')
  })

  it('主动操作失败保留独立反馈，已受理任务保留待确认状态', () => {
    expect(errorDisposition(network, { intent: 'mutation' }, offline)).toBe('local')
    expect(errorDisposition(network, { intent: 'uncertain' }, offline)).toBe('uncertain')
    expect(errorDisposition(network, { intent: 'uncertain' }, online)).toBe('local')
  })

  it('仅收口对应账户且已有异常入口的渠道错误', () => {
    const failure = new ApiError(503, '渠道断线', { body: { detail: { error_scope: 'account_connection' } } })
    const environment = { disconnected: false, account: { accountId: 7, handled: true } }
    expect(errorDisposition(failure, { accountId: 7 }, environment)).toBe('unavailable')
    expect(errorDisposition(failure, { accountId: 7, hasData: true }, environment)).toBe('stale')
    expect(errorDisposition(failure, { accountId: 8 }, environment)).toBe('local')
    expect(errorDisposition(failure, {}, environment)).toBe('local')
    expect(errorDisposition(failure, { accountId: 7 }, { ...environment, account: { accountId: 7, handled: false } })).toBe('local')
    expect(errorDisposition(failure, { accountId: 7, intent: 'mutation' }, environment)).toBe('local')
  })

  it('取消和无错误不占用注意力', () => {
    expect(errorDisposition(new DOMException('取消', 'AbortError'), { intent: 'mutation' }, offline)).toBe('none')
    expect(errorDisposition(null, {}, offline)).toBe('none')
  })
})

it('连接操作可选择由全局承接网络错误，独立响应仍保留', () => {
  for (const intent of ['read', 'mutation', 'uncertain'] as const) {
    const options = { intent, networkFeedback: 'global-only' as const }
    expect(errorDisposition(network, options, offline)).toBe('none')
    expect(errorDisposition(network, options, online)).toBe('local')
    for (const status of [422, 500, 503]) {
      expect(errorDisposition(new ApiError(status, '独立错误'), options, offline)).toBe('local')
    }
  }
})
