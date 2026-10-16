import { useEffect, useState } from 'react'
import type { ControlFrame } from '@/stores/liveControl'
import { controlQuotaState, type QuotaWarning } from '@/features/account/controlWarnings'

/** 常驻提示在采样失败时保留最后已知状态；跨账户和统计日不沿用。 */
export function useControlQuotaWarnings(frame: ControlFrame | undefined): QuotaWarning[] {
  const [last, setLast] = useState<{ accountId: number; date: string | null; timezone: string | null; warnings: QuotaWarning[] } | null>(null)
  const current = controlQuotaState(frame)
  useEffect(() => {
    const state = controlQuotaState(frame)
    if (frame && state.reliable) setLast({ accountId: frame.account_id, date: frame.control_date, timezone: frame.timezone, warnings: state.warnings })
  }, [frame])
  if (current.reliable) return current.warnings
  if (frame && last?.accountId === frame.account_id && last.date === frame.control_date && last.timezone === frame.timezone) {
    return last.warnings.map(warning => ({ ...warning, stale: true }))
  }
  return current.warnings.map(warning => ({ ...warning, stale: true }))
}
