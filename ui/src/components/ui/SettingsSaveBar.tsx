import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { OverflowText } from '@/components/ui/OverflowText'

/** 设置内容区内的脏状态栏；常挂载，只用透明度表达出现与消失。 */
export function SettingsSaveBar({
  changes,
  blocked,
  saving,
  onCancel,
  onSave,
  error,
}: {
  changes: string[]
  blocked?: boolean
  saving?: boolean
  onCancel: () => void
  onSave: () => void
  error?: unknown | null
}) {
  const open = changes.length > 0
  return (
    <div
      inert={!open}
      aria-hidden={!open}
      className={`absolute inset-x-0 bottom-0 z-20 transition-opacity duration-200 motion-reduce:transition-none ${open ? 'opacity-100' : 'pointer-events-none opacity-0'}`}
    >
      <div className="mx-auto w-full max-w-[1728px] px-5 sm:px-12">
        <div className="border-t border-line bg-surface/90 px-4 py-3 backdrop-blur-md">
          <div className="flex items-center gap-4">
            <div className="min-w-0 flex-1">
              <div className="text-[12px] font-semibold tracking-wide text-ink-3">
                待保存 · {changes.length} 项
                {blocked && <span className="text-warn"> · 有错误</span>}
              </div>
              <OverflowText
                className="text-[14px] text-ink-1"
                text={changes.join(' · ')}
              />
            </div>
            <button
              type="button"
              className="flex-none cursor-pointer text-[14px] text-ink-2 hover:text-ink-1"
              onClick={onCancel}
            >
              取消修改
            </button>
            <button
              type="button"
              className="flex-none cursor-pointer rounded-[9px] border-0 bg-ink-1 px-5 py-2 text-[14.5px] font-semibold text-surface disabled:opacity-45"
              onClick={onSave}
              disabled={blocked || saving || !open}
            >
              {saving ? '重启中…' : '保存并重启'}
            </button>
          </div>
          <ErrorNotice intent="mutation"
            title="保存系统配置失败"
            error={error}
            variant="mutation"
            onRetry={onSave}
          />
        </div>
      </div>
    </div>
  )
}
