import { useRef } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import type { ValidationStepDetails } from '@/lib/api/connectionValidation'

/** 渠道只提供有序键值行；关闭时保留内容，直到共用弹窗完成过渡。 */
export function ValidationStepDetailsModal({ details, onClose }: {
  details: ValidationStepDetails | null
  onClose: () => void
}) {
  const lastDetails = useRef(details)
  if (details) lastDetails.current = details
  const shown = details ?? lastDetails.current
  return <CenteredModal open={details !== null} title={shown?.title ?? '验证步骤详情'} onClose={onClose}
    footer={<div className="flex justify-end border-t border-line px-[22px] py-4">
      <button type="button" onClick={onClose} className="cursor-pointer rounded-lg bg-ink-1 px-4 py-2 text-[13px] text-surface">关闭</button>
    </div>}>
    <table className="mb-3 w-full table-fixed text-left text-[13px]">
      <thead className="text-[12px] text-ink-3">
        <tr className="border-b border-line">
          <th scope="col" className="w-2/5 py-2 pr-4 font-normal">字段</th>
          <th scope="col" className="py-2 font-normal">值</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-line">
        {shown?.rows.map((row, index) => <tr key={index}>
          <th scope="row" className="whitespace-pre-wrap py-3 pr-4 align-top font-normal text-ink-3 [overflow-wrap:anywhere]">{row.key}</th>
          <td className="whitespace-pre-wrap py-3 align-top text-ink-1 [overflow-wrap:anywhere]">{row.value == null || row.value === '' ? '未返回' : String(row.value)}</td>
        </tr>)}
      </tbody>
    </table>
  </CenteredModal>
}
