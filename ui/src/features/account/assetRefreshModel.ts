import { ApiError } from '@/lib/api/client'

/** 资产刷新端点以 409 表示账户锁被执行或另一次刷新占用。 */
export function isAssetRefreshBusy(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

export function assetRefreshFeedback(manualError: unknown, automaticError: unknown) {
  if (manualError != null) return { error: manualError, busy: isAssetRefreshBusy(manualError) }
  return { error: isAssetRefreshBusy(automaticError) ? null : automaticError, busy: false }
}
