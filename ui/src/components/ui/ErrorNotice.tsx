import { ModalContentContext } from '@/components/ui/modalContext'
import { useContext, useEffect, useMemo, useState } from 'react'
import { RefreshCw, TriangleAlert } from 'lucide-react'

import { ErrorDetailsModal } from '@/components/ui/ErrorDetailsModal'
import { ErrorCopyButton, ErrorDetailsContent } from '@/components/ui/ErrorDetailsContent'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Tooltip } from '@/components/ui/Tooltip'
import { errorInfo, type ErrorEvidence, type ErrorInfo } from '@/lib/errorInfo'
import { timeAgo } from '@/lib/format'
import { AccountConnectionContext, isAccountConnectionError } from '@/lib/accountConnection'

export type ErrorNoticeVariant = 'section' | 'compact' | 'mutation' | 'stale'

interface ErrorNoticeProps {
  title: string
  error: unknown | null
  variant?: ErrorNoticeVariant
  updatedAt?: number | null
  evidence?: ErrorEvidence[]
  embedded?: boolean
  contextKey?: string | number
  onRetry?: () => void | Promise<void>
}

/** 常挂错误槽：出现/收起只走 grid-fr 布局流，不额外叠加淡入或位移动效。 */
export function ErrorNotice({
  title,
  error,
  variant = 'section',
  updatedAt = null,
  evidence = [],
  onRetry,
  embedded = false,
  contextKey,
}: ErrorNoticeProps) {
  const inModal = useContext(ModalContentContext)
  const inlineDetails = embedded || inModal
  const accountErrorHandled = useContext(AccountConnectionContext)
  const hasError = error != null && !(accountErrorHandled && isAccountConnectionError(error))
  const current = useMemo(() => hasError ? errorInfo(error) : null, [error, hasError])
  const [retained, setRetained] = useState<ErrorInfo | null>(current)
  const [selection, setSelection] = useState<{ title: string; info: ErrorInfo } | null>(null)
  const open = hasError || (inlineDetails && selection !== null)
  useEffect(() => setSelection(null), [contextKey])

  useEffect(() => {
    if (current) setRetained(current)
  }, [current, open])

  const shown = current ?? retained
  const compact = variant === 'compact' || variant === 'stale'
  const blocked = variant === 'mutation'
  const label = variant === 'stale' && updatedAt != null
    ? `${title}，当前显示 ${timeAgo(updatedAt)}的数据`
    : title

  return (
    <div className={`grid transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
      <div className="min-h-0 overflow-hidden" inert={!open} aria-hidden={!open}>
        <div
          className={`${compact ? 'py-1.5 text-[13.5px]' : 'my-2 py-2 text-[14px]'} ${blocked ? 'rounded-[6px] bg-warn-soft px-2.5' : 'border-l-2 border-warn pl-2.5'}`}
          role={variant === 'mutation' ? 'alert' : 'status'}
          aria-live={variant === 'mutation' ? 'assertive' : 'polite'}
        >
          <div className="flex min-w-0 items-start gap-2">
            <TriangleAlert size={compact ? 14 : 16} className="mt-0.5 flex-none text-warn" aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="font-medium text-warn">{label}</div>
              {shown && <div className="mt-0.5 text-ink-2"><InkRewrite text={shown.message} tone="label" /></div>}
            </div>
            {onRetry && (
              <Tooltip content="重试">
                <button
                  type="button"
                  aria-label={`重试：${title}`}
                  className="flex-none cursor-pointer text-ink-3 hover:text-warn"
                  onClick={() => void onRetry()}
                >
                  <RefreshCw size={15} aria-hidden />
                </button>
              </Tooltip>
            )}
          </div>
          {hasError && <button type="button" className="mt-1 cursor-pointer text-[12.5px] text-ink-3 hover:text-ink-2"
            onClick={() => setSelection({ title, info: errorInfo(error, [{ label: '操作', value: title }, ...evidence]) })}>
            查看错误详情
          </button>}
          {inlineDetails && selection && <div className="mt-3">
            <div className="max-h-[40vh] overflow-y-auto"><ErrorDetailsContent info={selection.info} /></div>
            <ErrorCopyButton text={selection.info.text} />
            <button type="button" onClick={() => setSelection(null)} className="ml-3 cursor-pointer text-[13px] text-ink-3">收起详情</button>
          </div>}
          {!inlineDetails && selection && <ErrorDetailsModal selection={selection} onClose={() => setSelection(null)} />}

        </div>
      </div>
    </div>
  )
}
