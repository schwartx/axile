import { LoaderCircle, RadioTower } from 'lucide-react'
import { InkRewrite } from '@/components/ui/InkRewrite'

export function ValidationCardHeader({ title, status, statusClassName = 'text-ink-3', statusBadge = false, action, disabled, loading, onAction }: {
  title: string
  status: string
  statusClassName?: string
  statusBadge?: boolean
  action: string
  disabled: boolean
  loading?: boolean
  onAction?: () => void
}) {
  return <div className={`flex justify-between gap-3 ${statusBadge ? 'items-center' : 'items-start'}`}>
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <h2 className="flex shrink-0 items-center gap-2 text-[15px] font-[620] text-ink-1">
          <RadioTower size={16} className="shrink-0" aria-hidden="true" />{title}
        </h2>
        {statusBadge && <span role="status" title={status || undefined}
          className={status ? 'truncate rounded-full bg-fill px-2 py-0.5 text-xs text-ink-2' : 'sr-only'}>{status}</span>}
      </div>
      {!statusBadge && status && <div role="status" className="mt-1 min-h-[18px] text-xs">
        <InkRewrite tone="prose" text={status} textClassName={statusClassName} />
      </div>}
    </div>
    <button type={onAction ? 'button' : 'submit'} onClick={onAction} disabled={disabled}
      className="relative flex h-[34px] w-[100px] shrink-0 cursor-pointer items-center justify-center rounded-lg bg-ink-1 px-2 text-[13px] font-[550] text-surface transition-opacity duration-200 motion-reduce:transition-none disabled:cursor-not-allowed disabled:opacity-45">
      {loading && <LoaderCircle size={12} className="absolute left-1 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
      <InkRewrite text={action} />
    </button>
  </div>
}
