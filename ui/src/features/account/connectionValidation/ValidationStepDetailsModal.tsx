import { useRef } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import type { ValidationKeyValueDetails } from '@/lib/api/connectionValidation'

/** 窄标签列保留渠道字段顺序；关闭时保留内容，直到共用弹窗完成过渡。 */
export function ValidationStepDetailsModal({ details, onClose }: {
  details: ValidationKeyValueDetails | null
  onClose: () => void
}) {
  const lastDetails = useRef(details)
  if (details) lastDetails.current = details
  const shown = details ?? lastDetails.current
  return <CenteredModal open={details !== null} title={shown?.title ?? '验证步骤详情'} onClose={onClose}>
    <dl className="space-y-3 pt-1 pb-4 text-[13px] leading-6">
      {shown?.rows.map((row, index) => {
        const missing = row.value == null || row.value === ''
        return <div key={index} className="grid grid-cols-[104px_minmax(0,1fr)] gap-x-5">
          <dt className="whitespace-pre-wrap text-ink-2 [overflow-wrap:anywhere]">{row.key}</dt>
          <dd className={`min-w-0 whitespace-pre-wrap tabular-nums [overflow-wrap:anywhere] ${missing ? 'text-ink-3' : 'text-ink-1'}`}>
            {missing ? '未返回' : String(row.value)}
          </dd>
        </div>
      })}
    </dl>
  </CenteredModal>
}
