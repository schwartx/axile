import { expect, test } from 'bun:test'
import { ApiError } from '@/lib/api/client'
import { isAccountConnectionError } from '@/lib/accountConnection'

test('只接受后端明确的连接归属，不从消息或 HTTP 状态猜测', () => {
  expect(isAccountConnectionError(new ApiError(502, '账户连接失败'))).toBe(false)
  expect(isAccountConnectionError(new Error('account_connection'))).toBe(false)
  expect(isAccountConnectionError(new ApiError(502, '查询拒绝', { body: { detail: { error_scope: 'operation' } } }))).toBe(false)
  expect(isAccountConnectionError(new ApiError(502, '初始化失败', { body: { detail: { error_scope: 'account_connection' } } }))).toBe(true)
  expect(isAccountConnectionError(new ApiError(502, '初始化失败', { body: { error_scope: 'account_connection' } }))).toBe(true)
})
