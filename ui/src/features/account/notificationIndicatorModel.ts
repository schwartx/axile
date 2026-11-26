import type { Account } from '@/types/api'
import { SIGNAL_CHANNEL } from '@/features/signal/signalData'

export interface NotificationIndicatorProps {
  account: Pick<Account, 'id' | 'execution_notification_status' | 'notification_state'> & Partial<Pick<Account, 'trade_channel'>>
  executionId?: string | null
}

export function notificationIndicator({ account, executionId }: NotificationIndicatorProps) {
  const mode = account.execution_notification_status
  const signal = account.trade_channel === SIGNAL_CHANNEL
  if (mode === 'none') return { label: '通知未设置', result: null, detail: signal ? '调仓通知未设置' : '点击设置执行通知' }
  const state = account.notification_state
  const cancelled = state?.last_attempt_event_type === 'supplement.cancelled'
  const matches = signal ? state?.last_attempt_event_type === 'signal.calculated'
    : cancelled || (executionId != null && state?.last_attempt_execution_id === executionId)
  const result = matches ? state?.last_attempt_ok ?? null : null
  const label = result === false
    ? mode === 'default' ? '飞书通知发送失败' : '通知函数执行失败'
    : mode === 'function' ? '自定义' : ''
  const eventLabel = signal ? '最近一次调仓信号' : cancelled ? '补发取消' : '最近执行'
  const kind = mode === 'default' ? '默认飞书通知' : '自定义通知函数'
  const detail = result === true
    ? `${kind}：${eventLabel}的通知函数成功返回（${state?.last_attempt_at}）`
    : result === false
      ? `${kind}：${state?.last_attempt_at} · ${state?.last_attempt_error || '执行失败'}`
      : `${kind}：${eventLabel}的通知结果尚未确认`
  return { label, result, detail }
}

