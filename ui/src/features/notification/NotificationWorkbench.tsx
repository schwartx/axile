/** 账户与系统通知共用工作台；页面只提供各自的数据、保存与试跑契约。 */
import { useRef, useState, type ReactNode } from 'react'
import { Check, Circle, Play, TriangleAlert } from 'lucide-react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { PYTHON_RUN_STYLE, pythonRunStatus, type PythonEditorHandle, type PythonValidationState } from '@/components/ui/PythonFunctionEditor'
import { PythonWorkbench } from '@/components/ui/PythonWorkbench'
import { PythonWorkbenchPane } from '@/components/ui/PythonWorkbenchPane'
import { WorkbenchPanel } from '@/components/ui/WorkbenchPanel'
import type { PythonSaveAction } from '@/components/ui/pythonSaveAction'

export function NotificationWorkbench({ kind, storageKey, header, controls, code, onChange, defaultCode, onReset, running, result, message, stale, onRun, saveAction, docHref, runDisabled = false }: {
  kind: 'account_notification' | 'system_notification'
  storageKey: string
  header: ReactNode
  controls?: ReactNode
  code: string
  onChange: (code: string) => void
  defaultCode: string | null
  onReset: () => void
  running: boolean
  result: PythonValidationState | null
  message?: string
  stale: boolean
  onRun: () => void
  saveAction: PythonSaveAction
  docHref: string
  runDisabled?: boolean
}) {
  const [resultOpen, setResultOpen] = useState(true)
  const [editorStatus, setEditorStatus] = useState<HTMLDivElement | null>(null)
  const [resizing, setResizing] = useState(false)
  const editorRef = useRef<PythonEditorHandle>(null)
  const status = pythonRunStatus(running, result, stale)
  const style = PYTHON_RUN_STYLE[status]
  const busy = running || saveAction.saving || runDisabled
  const run = () => { setResultOpen(true); onRun() }
  return (
    <PythonWorkbench
      storageKey={`${storageKey}.inspectorWidth`}
      resizingPanels={resizing}
      saveAction={{ ...saveAction, onRestore: () => { saveAction.onRestore?.(); editorRef.current?.focus() } }}
      status={<div ref={setEditorStatus} className="mr-2 flex-none" />}
      inspector={(
        <div className={`grid min-h-0 content-start transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${resizing ? '!transition-none' : ''}`} style={{ gridTemplateRows: resultOpen ? 'auto minmax(0,0fr) 36px minmax(0,1fr)' : 'auto minmax(0,1fr) 36px minmax(0,0fr)' }}>
          <aside className="flex min-h-0 w-full flex-col overflow-y-auto border-line bg-surface px-3 py-2.5 md:border-r">
            {header}
            <div className="mt-2 flex gap-3 text-[13px]">
              <button type="button" className="cursor-pointer text-accent disabled:opacity-45" disabled={defaultCode === null || busy} onClick={onReset}>重置为默认</button>
              <button type="button" className="cursor-pointer text-ink-2 disabled:opacity-45" disabled={!code.trim() || busy} onClick={() => onChange('')}>清空函数</button>
            </div>
            {controls}
            <button type="button" title="试跑会执行代码，可能向外发送消息" className="mt-4 inline-flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-[6px] border-0 bg-ink-1 px-3 py-1.5 text-[14px] font-[550] text-surface disabled:cursor-default disabled:opacity-45" disabled={!code.trim() || busy} onClick={run}>
              <Play size={14} aria-hidden /> <InkRewrite text={running ? '试跑中…' : '试跑函数'} tone="label" textClassName="text-surface" />
            </button>
          </aside>
          <div aria-hidden />
          <WorkbenchPanel title="试跑结果" open={resultOpen} onToggle={() => setResultOpen((open) => !open)} className="border-t border-line md:border-r" stale={stale} status={(
            <span className="ml-auto flex items-center gap-1.5 px-3.5 text-[12.5px]">
              {status === 'pass' ? <Check size={13} className="text-accent" /> : status === 'fail' ? <TriangleAlert size={13} className="text-warn" /> : <Circle size={7} className={running ? 'fill-accent text-accent' : 'fill-ink-3 text-ink-3'} />}
              <InkRewrite text={style.body} tone="label" textClassName={style.text} />
            </span>
          )}>
            {message ? <p className={`text-[13.5px] ${result?.valid ? 'text-ink-2' : 'text-warn'}`} role="status">{message}</p> : <p className="text-[13.5px] text-ink-3">尚未试跑</p>}
          </WorkbenchPanel>
        </div>
      )}
    >
      <PythonWorkbenchPane kind={kind} editorRef={editorRef} statusTarget={editorStatus} storageKey={`${storageKey}.editorSplit`} title="通知函数" dirty={saveAction.dirty} docHref={docHref} code={code} onChange={onChange} running={running} runDisabled={saveAction.saving || runDisabled} stale={stale} result={result} onRun={run} onResizeChange={setResizing} />
    </PythonWorkbench>
  )
}
