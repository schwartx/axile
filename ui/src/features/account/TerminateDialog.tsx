import { ActionModal } from '@/components/ui/ActionModal'
import type { ExecutionTerminateMode } from '@/lib/api/executions'

/** 终止前确认挂单处理方式；仅剩补发时直接确认取消补发。 */
export function TerminateDialog({ open, running, name, disabled, onClose, onConfirm }: {
  open: boolean
  running: boolean
  name: string
  disabled: boolean
  onClose: () => void
  onConfirm: (mode: ExecutionTerminateMode) => void
}) {
  const button = 'min-h-[88px] w-full cursor-pointer rounded-xl border px-6 py-5 text-left text-[20px] font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default disabled:opacity-45'
  const blocked = disabled || !open
  return <ActionModal open={open} label={`${name}：${running ? '终止执行' : '取消剩余补发'}`} onClose={onClose}>
    {running ? <>
      <button type="button" className={`${button} border-warn/40 text-warn hover:bg-warn/10`} disabled={blocked} onClick={() => onConfirm('cancel_pending')}>停止并撤单</button>
      <button type="button" className={`${button} border-line text-ink-1 hover:bg-fill`} disabled={blocked} onClick={() => onConfirm('graceful')}>停止，保留挂单</button>
    </> : <>
      <button type="button" className={`${button} border-line text-ink-2 hover:bg-fill`} onClick={onClose}>返回</button>
      <button type="button" className={`${button} border-warn/40 text-warn hover:bg-warn/10`} disabled={blocked} onClick={() => onConfirm('graceful')}>取消剩余补发</button>
    </>}
  </ActionModal>
}
