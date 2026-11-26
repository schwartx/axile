import { Bell, Check, X } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { Tooltip } from '@/components/ui/Tooltip'
import { SIGNAL_CHANNEL } from '@/features/signal/signalData'
import { notificationIndicator, type NotificationIndicatorProps } from './notificationIndicatorModel'

/** 通知入口，悬停补充通知方式与结果详情。 */
export function NotificationIndicator(props: NotificationIndicatorProps) {
  const { label, result, detail } = notificationIndicator(props)
  const signal = props.account.trade_channel === SIGNAL_CHANNEL
  const configured = props.account.execution_notification_status !== 'none'
  const className = `inline-flex shrink-0 items-center gap-1.5 rounded-sm text-[13px] no-underline hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-4 ${result === false ? 'text-warn' : 'text-ink-3'}`
  const icon = (
    <span className="relative inline-flex h-5 w-5 items-center justify-center" aria-hidden="true">
      <Bell size={13} />
      {result !== null && (
        <span className="absolute -bottom-0.5 -right-0.5 rounded-full bg-surface p-px">
          {result ? <Check size={10} strokeWidth={3} /> : <X size={10} strokeWidth={3} />}
        </span>
      )}
    </span>
  )
  return (
    <Tooltip content={detail}>
      <Link to={`/accounts/${props.account.id}/edit/notification`}
        aria-label={signal ? `编辑调仓通知：${detail}` : `${detail}，编辑通知`}
        className={signal ? `${className} min-h-6 min-w-6 justify-center` : className}>
        {(signal || configured) && icon}
        {!signal && label && <span>{label}</span>}
      </Link>
    </Tooltip>
  )
}
