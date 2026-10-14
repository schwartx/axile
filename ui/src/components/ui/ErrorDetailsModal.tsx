import { CenteredModal } from '@/components/ui/CenteredModal'
import { ErrorCopyButton, ErrorDetailsContent } from '@/components/ui/ErrorDetailsContent'
import type { ErrorInfo } from '@/lib/errorInfo'

export function ErrorDetailsModal({ selection, onClose, copyLabel = '复制错误信息' }: {
  selection: { title: string; info: ErrorInfo } | null
  onClose: () => void
  copyLabel?: string
}) {
  return <CenteredModal open={selection !== null} title={selection?.title ?? '错误详情'} onClose={onClose} widthClassName="w-[1200px]"
    footer={<div className="flex flex-wrap items-center justify-end gap-3 border-t border-line px-[22px] py-4">
      {selection && <ErrorCopyButton text={selection.info.text} label={copyLabel} />}
      <button type="button" onClick={onClose} className="cursor-pointer rounded-lg bg-ink-1 px-4 py-2 text-[13px] text-surface">关闭详情</button>
    </div>}>
    {selection && <ErrorDetailsContent info={selection.info} />}
  </CenteredModal>
}
