import { ErrorNotice } from '@/components/ui/ErrorNotice'
import type { PythonChecks } from '@/components/ui/pythonChecks'
import { checksAreEmpty } from '@/components/ui/pythonChecks'
import { useErrorPresentation } from '@/lib/errorPresentation'

function SourceStatus({ source, checks, onRetry }: { source: 'lsp' | 'contract'; checks: PythonChecks | null; onRetry?: () => void }) {
  const state = checks?.[source]
  const failure = useErrorPresentation(state?.error)
  const name = source === 'lsp' ? 'LSP 诊断' : '入口契约检查'
  return <div>
    <span>{name}：{state?.status === 'succeeded' ? `检查完成 · ${state.diagnostics.length} 条诊断` : state?.status === 'failed' ? failure.local ? '检查失败' : '检查暂不可用' : '检查中…'}</span>
    <ErrorNotice intent="read" title={`${name}失败`} error={state?.error} evidence={checks ? [{ label: '文档版本', value: String(checks.version) }, { label: '请求代次', value: String(checks.generation) }] : []} onRetry={onRetry} contextKey={checks?.version} />
  </div>
}

export function PythonCheckStatus({ checks, onRetry, onQuickFixRetry }: { checks: PythonChecks | null; onRetry?: () => void; onQuickFixRetry?: () => void }) {
  return <div className="px-3.5 py-2 text-[13px] text-ink-3" aria-live="polite">
    {(['lsp', 'contract'] as const).map(source => <SourceStatus key={source} source={source} checks={checks} onRetry={onRetry} />)}
    <ErrorNotice intent="read" title="快速修复暂不可用" error={checks?.quickFixError} onRetry={onQuickFixRetry} contextKey={checks?.version}
      evidence={checks ? [{ label: '文档版本', value: String(checks.version) }] : []} />
    {checksAreEmpty(checks) && <p className="mt-1">暂无静态诊断。</p>}
  </div>
}
