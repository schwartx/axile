import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { python } from '@codemirror/lang-python'
import { Copy, X } from 'lucide-react'
import { pythonEditorTheme } from '@/components/ui/pythonEditorTheme'
import { OverflowText } from '@/components/ui/OverflowText'
import { useToastStore } from '@/stores/ui'
import type { PortfolioLite } from '@/types/api'

const CodeMirror = lazy(() => import('@uiw/react-codemirror'))
const extensions = [...pythonEditorTheme, python()]

/** 原生模态框负责焦点约束、Escape 和焦点归还，预览不改变向导选择。 */
export function PortfolioCodePreview({ portfolio, selected, onClose, onSelect }: {
  portfolio: PortfolioLite
  selected: boolean
  onClose: () => void
  onSelect: () => void
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [closing, setClosing] = useState(false)
  const closeAction = useRef<(() => void) | null>(null)
  const toast = useToastStore((state) => state.toast)
  const code = portfolio.custom_calc_py_code ?? ''

  useEffect(() => {
    const dialog = dialogRef.current!
    const trigger = document.activeElement as HTMLElement | null
    dialog.showModal()
    return () => {
      dialog.close()
      trigger?.focus({ preventScroll: true })
    }
  }, [])

  // 关闭仍留在 top layer；过渡结束后才提交选择或卸载，焦点由清理逻辑归还。
  const dismiss = (action: () => void = onClose) => {
    if (closeAction.current) return
    closeAction.current = action
    setClosing(true)
  }

  useEffect(() => {
    if (!closing) return
    let active = true
    const animations = dialogRef.current!.getAnimations()
    void Promise.allSettled(animations.map((animation) => animation.finished)).then(() => {
      if (active) closeAction.current?.()
    })
    return () => { active = false }
  }, [closing])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      toast('代码已复制')
    } catch {
      toast('复制失败，请选中代码手动复制')
    }
  }

  return (
    <dialog ref={dialogRef} aria-labelledby="portfolio-code-title" onCancel={(event) => { event.preventDefault(); dismiss() }}
      onClick={(event) => { if (event.target === event.currentTarget) dismiss() }}
      data-closing={closing || undefined}
      className="code-preview-dialog fixed inset-0 m-auto h-[min(760px,88dvh)] max-h-[88dvh] w-[min(1100px,94vw)] max-w-[94vw] overflow-hidden rounded-[18px] border border-line bg-surface p-0 text-ink-1 shadow-xl">
      <div inert={closing} className="flex h-full min-h-0 flex-col">
        <div className="flex flex-none items-center gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 id="portfolio-code-title" className="text-[17px] font-[620]"><OverflowText text={portfolio.name} /></h2>
            <p className="mt-1 text-[13px] text-ink-3">{portfolio.market} · 策略代码 · 只读</p>
          </div>
          <button type="button" onClick={() => void copy()} disabled={!code.trim()} className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-sm text-ink-2 focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-45"><Copy size={15} aria-hidden />复制</button>
          <button type="button" autoFocus aria-label="关闭代码预览" onClick={() => dismiss()} className="rounded-lg p-2 text-ink-2 hover:bg-fill focus-visible:ring-2 focus-visible:ring-accent"><X size={20} aria-hidden /></button>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden bg-code-bg">
          {code.trim() ? <Suspense fallback={<p className="p-5 text-sm text-ink-3">加载代码预览…</p>}>
            <CodeMirror value={code} readOnly theme="none" extensions={extensions}
              basicSetup={{ lineNumbers: true, foldGutter: false, highlightActiveLine: false, highlightActiveLineGutter: false, autocompletion: false, tabSize: 4 }}
              height="100%" className="h-full [&_.cm-editor]:h-full [&_.cm-scroller]:overflow-auto" aria-label={`${portfolio.name}的策略代码`} />
          </Suspense> : <p className="p-5 text-sm text-ink-3">此组合尚无策略代码</p>}
        </div>
        <div className="flex flex-none items-center justify-between gap-3 border-t border-line px-5 py-3.5">
          <span className="text-sm text-ink-3">{selected ? '当前已选择此组合' : '查看代码不会改变当前选择'}</span>
          <button type="button" onClick={() => dismiss(selected ? onClose : onSelect)} disabled={portfolio.id == null}
            className="flex-none rounded-lg bg-ink-1 px-4 py-2 text-sm font-medium text-surface focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-45">{selected ? '完成' : '选择此组合'}</button>
        </div>
      </div>
    </dialog>
  )
}
