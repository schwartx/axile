import { ReadStatusDot } from '@/features/account/ReadStatusDot'
import type { AccountAssetRefresh } from '@/lib/api/orders'

export function AssetRefreshStatus({ result }: { result?: AccountAssetRefresh | null }) {
  if (!result || result.status === 'updated') return null
  const label = result.message === '正在核对持仓' ? result.message : '持仓待核对'
  return <span role="status" className="inline-flex items-center text-xs text-ink-3">{label}
    <ReadStatusDot label={label}>{result.message ?? '本次持仓核对尚未完成，当前显示上次资产快照'}</ReadStatusDot>
  </span>
}
