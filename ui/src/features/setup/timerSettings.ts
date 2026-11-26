import { useState } from 'react'
import { runtimeSyncMessage } from '@/features/account/useAccountRuntimeSync'
import { updateAccount, type AccountUpdatePayload } from '@/lib/api/accounts'
import { useToastStore } from '@/stores/ui'
import type { Account } from '@/types/api'
import { cronExprEqual, gmTimerSessions, type GMSessions, type Supplement } from './cron'

type TimerSettingsChange = { patch: AccountUpdatePayload; message: string }

export function timerSettingsChange(
  signal: boolean,
  scheduleChannel: string,
  current: { cronExpr: string; isStarted?: boolean; supplement?: Supplement | null; channelOptions?: Account['channel_options'] },
  next: { cronExpr: string; autoOn: boolean; supplement: Supplement | null; gmSessions: GMSessions | null },
): TimerSettingsChange | null {
  const enabledChanged = signal && next.autoOn !== (Boolean(current.isStarted) && Boolean(current.cronExpr.trim()))
  const cronChanged = (!signal || next.autoOn) && !cronExprEqual(next.cronExpr, current.cronExpr)
  const supplementChanged = !signal && JSON.stringify(next.supplement) !== JSON.stringify(current.supplement ?? null)
  const sessionsChanged = JSON.stringify(next.gmSessions)
    !== JSON.stringify(gmTimerSessions(scheduleChannel, { gm_sessions: current.channelOptions?.gm }))
  if (!enabledChanged && !cronChanged && !supplementChanged && !sessionsChanged) return null

  const patch: AccountUpdatePayload = signal
    ? { ...(enabledChanged ? { is_started: next.autoOn } : {}), ...(cronChanged ? { cron_expr: next.cronExpr } : {}) }
    : { cron_expr: next.cronExpr, supplement: next.supplement }
  if (sessionsChanged && next.gmSessions) patch.channel_options = { ...current.channelOptions, gm: next.gmSessions }
  const message = enabledChanged ? (next.autoOn ? '已启动' : '已暂停')
    : signal || next.cronExpr ? '节奏已更新' : '已关闭自动调仓节奏'
  return { patch, message }
}

export function useTimerSave(accountId: number) {
  const toast = useToastStore((s) => s.toast)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const save = async (change: TimerSettingsChange, onSaved: (updated: Account) => void) => {
    if (saving) return
    setSaving(true)
    setError(null)
    try {
      const updated = await updateAccount(accountId, change.patch)
      toast(updated.runtime_sync?.status === 'synchronized' ? change.message : runtimeSyncMessage(updated.runtime_sync))
      onSaved(updated)
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      setSaving(false)
    }
  }
  return { saving, error, setError, save }
}
