import type { ReactNode } from 'react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { canSavePython, type PythonSaveAction } from '@/components/ui/pythonSaveAction'

/** 组合工作台的保存状态栏，嵌入式编辑器也复用相同状态和还原行为。 */
export function PythonSaveBar({ action, status }: { action: PythonSaveAction; status?: ReactNode }) {
  const warning = Boolean(action.error || action.blocked)
  const text = action.error
    ? '保存失败'
    : action.saving
      ? '保存中…'
      : action.blockedReason ?? (action.dirty ? '未保存到服务器' : '已保存到服务器')

  return (
    <footer className="flex h-7 flex-none items-center border-t border-line bg-surface px-2 text-[12px]">
      <div
        className={`flex min-w-0 flex-1 items-center gap-1.5 ${warning ? 'text-warn' : 'text-ink-2'}`}
        role={action.error ? 'alert' : 'status'}
        title={action.error?.message}
      >
        <span className={warning ? 'text-warn' : action.dirty ? 'text-accent' : 'text-ink-3'} aria-hidden>
          {warning ? '△' : action.dirty ? '●' : '✓'}
        </span>
        <InkRewrite text={text} tone="label" />
      </div>
      {status}
      <div className="flex h-full flex-none items-center">
        {action.onRestore && (
          <button
            type="button"
            className={`h-full px-2 text-ink-2 transition-opacity duration-200 motion-reduce:transition-none hover:bg-fill hover:text-ink-1 ${action.dirty ? 'cursor-pointer opacity-100' : 'pointer-events-none opacity-0'}`}
            disabled={action.saving || action.disabled}
            onClick={action.onRestore}
            tabIndex={action.dirty ? 0 : -1}
            aria-hidden={!action.dirty}
          >
            还原
          </button>
        )}
        <button
          type="button"
          title={action.error?.message ?? action.title ?? '保存（⌘/Ctrl+S）'}
          className="h-full px-2 font-[550] text-ink-1 hover:bg-fill disabled:cursor-default disabled:text-ink-3"
          disabled={!canSavePython(action)}
          onClick={action.onSave}
        >
          保存
        </button>
      </div>
    </footer>
  )
}
