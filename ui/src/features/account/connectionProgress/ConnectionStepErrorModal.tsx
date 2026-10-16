import { ErrorDetailsModal } from '@/components/ui/ErrorDetailsModal'
import { errorInfoFromEntries } from '@/lib/errorInfo'
import type { ConnectionStepError } from '@/lib/api/connectionProgress'

export interface ConnectionErrorSelection {
  stage: string
  error: ConnectionStepError
}

/** 柜台错误沿用原文、原因代码和柜台错误码，与请求错误共用详情组件。 */
export function ConnectionStepErrorModal({ selection, onClose }: {
  selection: ConnectionErrorSelection | null
  onClose: () => void
}) {
  const info = selection ? errorInfoFromEntries(selection.error.message, [
    { label: '阶段', value: selection.stage },
    { label: '原因代码', value: selection.error.reason_code },
    ...(selection.error.error_id != null ? [{ label: '柜台错误码', value: String(selection.error.error_id) }] : []),
  ], [
    { label: '错误说明', value: selection.error.message },
    { label: '错误原文', value: selection.error.details || selection.error.message },
    ...(selection.error.traceback ? [{ label: 'traceback', value: selection.error.traceback }] : []),
  ]) : null
  return <ErrorDetailsModal selection={info ? { title: '连接步骤错误详情', info } : null} onClose={onClose} />
}
