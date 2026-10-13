import { useEffect, useState } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { InkRewrite } from '@/components/ui/InkRewrite'
import type { ConnectionValidationError } from '@/lib/api/connectionValidation'

export interface ValidationErrorSelection {
  stage: string
  error: ConnectionValidationError
}

/** 完整详情自然换行、正文滚动，复制与关闭始终留在可见区域。 */
export function ConnectionValidationErrorModal({ selection, onClose }: {
  selection: ValidationErrorSelection | null
  onClose: () => void
}) {
  const [copyState, setCopyState] = useState('复制错误信息')
  useEffect(() => { setCopyState('复制错误信息') }, [selection])
  const error = selection?.error
  const details = error?.details || error?.message || ''
  const copy = async () => {
    if (!selection) return
    const content = [selection.stage, error?.message, `原因代码：${error?.reason_code}`,
      error?.error_id != null ? `柜台错误码：${error.error_id}` : null, '', details].filter(value => value != null).join('\n')
    try {
      await navigator.clipboard.writeText(content)
      setCopyState('已复制')
    } catch {
      setCopyState('复制失败，请选中文字复制')
    }
  }
  return <CenteredModal open={selection !== null} title="连接验证错误详情" subtitle={selection?.stage}
    onClose={onClose} widthClassName="w-[560px]"
    footer={<div className="flex flex-wrap items-center justify-end gap-3 border-t border-line px-[22px] py-4">
      <button type="button" onClick={() => void copy()} className="cursor-pointer rounded-lg border border-line px-3 py-2 text-[13px] text-ink-2"><InkRewrite text={copyState} /></button>
      <button type="button" onClick={onClose} className="cursor-pointer rounded-lg bg-ink-1 px-4 py-2 text-[13px] text-surface">关闭详情</button>
    </div>}>
    <dl className="mb-4 space-y-2 text-[13px]">
      <div><dt className="text-ink-3">原因代码</dt><dd className="mt-1 break-all font-mono">{error?.reason_code}</dd></div>
      {error?.error_id != null && <div><dt className="text-ink-3">柜台错误码</dt><dd className="mt-1 font-mono">{error.error_id}</dd></div>}
    </dl>
    <div className="mb-4 rounded-xl border border-line bg-fill p-3">
      <div className="mb-2 text-[12px] text-ink-3">错误原文</div>
      <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[13px] leading-relaxed text-ink-1">{details}</pre>
    </div>
  </CenteredModal>
}
