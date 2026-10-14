import { ErrorNotice } from '@/components/ui/ErrorNotice'
import type { PythonChecks } from '@/components/ui/pythonChecks'
import { checksAreEmpty } from '@/components/ui/pythonChecks'

export function PythonCheckStatus({ checks, onRetry, onQuickFixRetry }: { checks: PythonChecks | null; onRetry?: () => void; onQuickFixRetry?: () => void }) {
  return <div className="px-3.5 py-2 text-[13px] text-ink-3" aria-live="polite">
    {(['lsp', 'contract'] as const).map(source => {
      const state = checks?.[source]
      const name = source === 'lsp' ? 'LSP 诊断' : '入口契约检查'
      return <div key={source}>
        <span>{name}：{state?.status === 'succeeded' ? `检查完成 · ${state.diagnostics.length} 条诊断` : state?.status === 'failed' ? '检查失败' : '检查中…'}</span>
        <ErrorNotice title={`${name}失败`} error={state?.error} evidence={checks ? [{ label: '文档版本', value: String(checks.version) }, { label: '请求代次', value: String(checks.generation) }] : []} onRetry={onRetry} contextKey={checks?.version} />
      </div>
    })}
    <ErrorNotice title="快速修复暂不可用" error={checks?.quickFixError} onRetry={onQuickFixRetry} contextKey={checks?.version}
      evidence={checks ? [{ label: '文档版本', value: String(checks.version) }] : []} />
    {checksAreEmpty(checks) && <p className="mt-1">暂无静态诊断。</p>}
  </div>
}
