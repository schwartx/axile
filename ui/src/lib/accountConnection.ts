import { createContext } from 'react'
import { ApiError } from '@/lib/api/client'

/** 仅在当前账户异常已有统一入口时，收起功能区域中的同源错误。 */
export const AccountConnectionContext = createContext(false)

export function isAccountConnectionError(error: unknown): boolean {
  if (!(error instanceof ApiError) || !error.body || typeof error.body !== 'object') return false
  const body = error.body as Record<string, unknown>
  const detail = body.detail && typeof body.detail === 'object' ? body.detail as Record<string, unknown> : {}
  return (body.error_scope ?? detail.error_scope) === 'account_connection'
}
