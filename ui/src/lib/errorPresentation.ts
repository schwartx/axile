import { createContext, useContext } from 'react'
import { AccountConnectionContext, isAccountConnectionError } from '@/lib/accountConnection'
import { isConnectionError } from '@/lib/errorInfo'
import { useDomainStore } from '@/stores/domain'

export type ErrorIntent = 'read' | 'mutation' | 'uncertain'
export type ErrorDisposition = 'none' | 'local' | 'stale' | 'unavailable' | 'uncertain'

/** 只有实际提供全局连接入口的布局才能收起局部网络错误。 */
export const GlobalErrorContext = createContext(false)

export interface ErrorPresentationOptions {
  intent?: ErrorIntent
  hasData?: boolean
  accountId?: number
}

interface ErrorEnvironment {
  disconnected: boolean
  account: { accountId: number; handled: boolean } | null
}

/** 展示归属独立于视觉样式；主动操作的结果始终保留。 */
export function errorDisposition(error: unknown, options: ErrorPresentationOptions, environment: ErrorEnvironment): ErrorDisposition {
  if (error == null || (error instanceof Error && error.name === 'AbortError')) return 'none'
  const intent = options.intent ?? 'read'
  if (intent === 'mutation') return 'local'
  const networkHandled = environment.disconnected && isConnectionError(error)
  if (intent === 'uncertain') return networkHandled ? 'uncertain' : 'local'
  const accountHandled = environment.account?.handled
    && options.accountId === environment.account.accountId && isAccountConnectionError(error)
  if (networkHandled || accountHandled) return options.hasData ? 'stale' : 'unavailable'
  return 'local'
}

export function useServiceDisconnected(): boolean {
  const hasGlobalEntry = useContext(GlobalErrorContext)
  const error = useDomainStore((state) => state.accountsError)
  return hasGlobalEntry && isConnectionError(error)
}

export function useErrorPresentation(error: unknown, options: ErrorPresentationOptions = {}) {
  const disconnected = useServiceDisconnected()
  const account = useContext(AccountConnectionContext)
  const disposition = errorDisposition(error, options, { disconnected, account })
  return { disposition, local: disposition === 'local', disconnected }
}
