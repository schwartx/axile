import { formatBeijingTimestamp, formatObservedAt } from '@/lib/scheduleTime'

export function AssetObservationTime({ observedAt }: { observedAt?: string | null }) {
  if (!observedAt) return '尚未查询账户资产'

  return (
    <time dateTime={observedAt} title={formatBeijingTimestamp(observedAt)}>
      资产查询于 {formatObservedAt(observedAt)}
    </time>
  )
}
