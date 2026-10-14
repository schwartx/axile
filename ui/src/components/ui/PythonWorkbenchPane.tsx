import { PythonCheckStatus } from '@/components/ui/PythonCheckStatus'
import type { PythonChecks } from '@/components/ui/pythonChecks'
import { useEffect, useRef, useState, type Ref } from 'react'
import { PythonFunctionEditor, PythonRunResultBody, type PythonEditorHandle, type PythonValidationState } from '@/components/ui/PythonFunctionEditor'
import { PythonRunPanel } from '@/components/ui/PythonRunPanel'
import { Segmented } from '@/components/ui/Segmented'
import { CircleX, TriangleAlert } from 'lucide-react'
import type { PythonEditorKind } from '@/components/ui/pythonEditorContract'

const DEFAULT_SPLIT = 0.35
type OutputTab = 'problems' | 'result'

function storedSplit(storageKey: string): number | null {
  if (typeof window === 'undefined') return null
  const stored = Number(window.localStorage.getItem(storageKey))
  return window.localStorage.getItem(storageKey) != null && Number.isFinite(stored) && stored >= 0 && stored <= 0.8 ? stored : null
}

/** 工作台的代码区、静态问题与试跑结果；业务页提供代码、执行行为和状态栏槽位。 */
export function PythonWorkbenchPane({
  kind,
  code, onChange, running, runDisabled = false, stale, result, onRun, editorRef, statusTarget, storageKey,
  title, docHref, onResizeChange,
}: {
  kind: PythonEditorKind
  code: string
  onChange: (code: string) => void
  running: boolean
  runDisabled?: boolean
  stale: boolean
  result: PythonValidationState | null
  onRun: () => void
  editorRef: Ref<PythonEditorHandle>
  statusTarget: HTMLElement | null
  storageKey: string
  title: string
  docHref?: string
  onResizeChange?: (resizing: boolean) => void
}) {
  const [problemsOpen, setProblemsOpen] = useState(false)
  const [checks, setChecks] = useState<PythonChecks | null>(null)
  const [activeTab, setActiveTab] = useState<OutputTab>('problems')
  const [savedSplit] = useState(() => storedSplit(storageKey))
  const [split, setSplit] = useState(savedSplit ?? DEFAULT_SPLIT)
  const [customSplit, setCustomSplit] = useState(savedSplit != null)
  const [resizing, setResizing] = useState(false)
  const [header, setHeader] = useState<HTMLDivElement | null>(null)
  const paneRef = useRef<HTMLDivElement>(null)
  const internalEditorRef = useRef<PythonEditorHandle>(null)
  const previousStaticCount = useRef(0)

  useEffect(() => {
    if (result) {
      setActiveTab('result')
      setProblemsOpen(true)
    }
  }, [result])

  const currentChecks = checks?.document === code ? checks : null
  const staticProblems = [...(currentChecks?.lsp.diagnostics ?? []), ...(currentChecks?.contract.diagnostics ?? [])].map(item => ({
    line: item.range.start.line + 1, message: typeof item.message === 'string' ? item.message : item.message.value,
    severity: item.severity === 1 ? 'error' : 'warning',
  }))
  const checkFailed = currentChecks?.lsp.status === 'failed' || currentChecks?.contract.status === 'failed'
  useEffect(() => { if (checkFailed && result == null) setProblemsOpen(true) }, [checkFailed, result])
  const outputTabs = [
    {
      value: 'problems' as const,
      label: '代码问题',
      badge: staticProblems.length > 0 || checkFailed ? <span className="rounded-full bg-warn/15 px-1.5 text-[10px] tabular-nums text-warn">{staticProblems.length || '!'}</span> : undefined,
    },
    { value: 'result' as const, label: '试跑结果' },
  ]
  useEffect(() => {
    if (staticProblems.length > previousStaticCount.current && !running && result == null) {
      setActiveTab('problems')
      setProblemsOpen(true)
    }
    previousStaticCount.current = staticProblems.length
  }, [staticProblems.length, running, result])

  const setResizingState = (value: boolean) => {
    setResizing(value)
    onResizeChange?.(value)
  }
  const splitAt = (clientY: number) => {
    const bounds = paneRef.current?.getBoundingClientRect()
    if (!bounds) return split
    const available = bounds.height - 5 - 36
    if (available <= 0) return split
    const minShare = Math.min(0.45, 36 / available)
    return Math.min(Math.max((bounds.bottom - clientY - 41) / available, minShare), 1 - minShare)
  }
  const currentSplit = () => {
    const available = (paneRef.current?.getBoundingClientRect().height ?? 0) - 41
    const panelHeight = paneRef.current?.querySelector('section[aria-label="代码检查"]')?.getBoundingClientRect().height ?? 0
    return available > 0 ? Math.max(0, panelHeight - 36) / available : split
  }
  const persistSplit = (value: number) => {
    setSplit(value)
    setCustomSplit(true)
    window.localStorage.setItem(storageKey, String(value))
  }
  const fitProblems = activeTab === 'problems' && !customSplit && !resizing
  const rows = problemsOpen
    ? fitProblems
      ? 'minmax(0, 1fr) 5px 36px fit-content(40%)'
      : `minmax(0, ${1 - split}fr) 5px 36px minmax(0, ${split}fr)`
    : 'minmax(0, 1fr) 0px 36px minmax(0, 0fr)'
  const run = () => {
    if (running || runDisabled || !code.trim()) return
    setActiveTab('result')
    setProblemsOpen(true)
    onRun()
  }

  return (
    <div ref={paneRef} className={`grid min-h-0 transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${resizing ? '!transition-none' : ''}`} style={{ gridTemplateRows: rows }}>
      <div className="flex min-h-[420px] min-w-0 flex-col bg-code-bg md:min-h-0">
        <div ref={setHeader} className="h-8 flex-none" />
        <PythonFunctionEditor
          kind={kind}
          ref={(handle) => {
            internalEditorRef.current = handle
            if (typeof editorRef === 'function') editorRef(handle)
            else if (editorRef) editorRef.current = handle
          }}
          headerTarget={header}
          statusTarget={statusTarget}
          onChecks={setChecks}
          layout="workbench"
          fill
          code={code}
          onChange={onChange}
          running={running}
          runDisabled={runDisabled}
          stale={stale}
          result={result}
          onRun={run}
          workbenchTitle={title}
          docHref={docHref}
        />
      </div>
      <div
        role="separator"
        aria-label="调整代码与问题的高度"
        aria-orientation="horizontal"
        aria-valuemin={0}
        aria-valuemax={80}
        aria-valuenow={fitProblems ? undefined : Math.round(split * 100)}
        aria-valuetext={fitProblems ? '代码问题按内容高度展开' : undefined}
        tabIndex={problemsOpen ? 0 : -1}
        inert={!problemsOpen}
        title="拖动分配代码与问题的高度 · 双击平均分配"
        className={`group relative z-10 cursor-row-resize touch-none outline-none ${problemsOpen ? 'block' : 'invisible'}`}
        onDoubleClick={() => persistSplit(DEFAULT_SPLIT)}
        onPointerDown={(event) => {
          if (!problemsOpen) return
          event.preventDefault()
          event.currentTarget.setPointerCapture(event.pointerId)
          setResizingState(true)
          setSplit(splitAt(event.clientY))
        }}
        onPointerMove={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) setSplit(splitAt(event.clientY))
        }}
        onPointerUp={(event) => {
          const next = splitAt(event.clientY)
          if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
          persistSplit(next)
          setResizingState(false)
        }}
        onPointerCancel={() => setResizingState(false)}
        onKeyDown={(event) => {
          let next = fitProblems ? currentSplit() : split
          if (event.key === 'ArrowUp') next += 0.05
          else if (event.key === 'ArrowDown') next -= 0.05
          else if (event.key === 'Home') next = 0
          else if (event.key === 'End') next = 0.8
          else return
          event.preventDefault()
          persistSplit(Math.min(Math.max(next, 0), 0.8))
        }}
      >
        <span className="absolute inset-x-0 top-1/2 h-px bg-line transition-colors duration-130 group-hover:bg-accent group-focus:bg-accent" />
      </div>
      <PythonRunPanel
        kind="problems"
        title="代码检查"
        hideTitle
        headerExtra={<Segmented size="sm" className="my-auto" value={activeTab} options={outputTabs} onChange={(tab) => { setActiveTab(tab); setProblemsOpen(true) }} />}
        statusOverride={activeTab === 'result' ? <span className="ml-auto self-center px-3 text-[12px] text-ink-3">{running ? '试跑中…' : stale ? '上次试跑结果' : result ? (result.valid ? '试跑通过' : '试跑失败') : '尚未试跑'}</span> : <span className="ml-auto self-center px-3 text-[12px] text-ink-3">{checkFailed ? '检查失败' : currentChecks?.lsp.status === 'succeeded' && currentChecks?.contract.status === 'succeeded' ? '检查完成' : '检查中…'}</span>}
        contentOverride={(
          activeTab === 'problems' ? (
            <div className="-mx-3.5 -my-3 divide-y divide-line/60">
              <PythonCheckStatus checks={currentChecks} onRetry={() => internalEditorRef.current?.check()} />
              {staticProblems.map((problem, index) => (
                <button key={`${index}-${problem.line}`} type="button" className="flex w-full cursor-pointer items-start gap-2.5 px-3.5 py-2 text-left transition-colors hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent focus-visible:-outline-offset-2 motion-reduce:transition-none" onClick={() => internalEditorRef.current?.revealLine(problem.line)}>
                  {problem.severity === 'error' ? <CircleX size={15} className="mt-0.5 flex-none text-warn" aria-hidden /> : <TriangleAlert size={15} className="mt-0.5 flex-none text-warn" aria-hidden />}
                  <span className="min-w-0 flex-1 text-[13px] leading-5 text-ink-1">{problem.message}</span>
                  <span className="flex-none whitespace-nowrap text-[11px] leading-5 text-ink-3">第 {problem.line} 行</span>
                </button>
              ))}

            </div>
          ) : (
            <div className="space-y-3">
              {stale && <p className="text-[12px] text-ink-3">代码已修改，以下为旧结果</p>}
              {!stale && result?.errorLine != null && <button type="button" className="text-[12px] text-accent" onClick={() => internalEditorRef.current?.revealLine(result.errorLine!)}>定位到第 {result.errorLine} 行</button>}
              {result ? <PythonRunResultBody result={result} stale={stale} /> : <p className="text-[13px] text-ink-3">{running ? '试跑中…' : '尚未试跑'}</p>}
            </div>
          )
        )}
        open={problemsOpen}
        onToggle={() => setProblemsOpen((open) => !open)}
        className="border-t border-line"
        running={running}
        result={result}
        stale={false}
        onRevealError={(line) => internalEditorRef.current?.revealLine(line)}
      />
    </div>
  )
}
