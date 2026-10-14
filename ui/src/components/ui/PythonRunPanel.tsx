import type { ReactNode } from 'react'
import { Check, Circle, TriangleAlert } from 'lucide-react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { WorkbenchPanel } from '@/components/ui/WorkbenchPanel'
import {
  PythonRunResultBody,
  PYTHON_RUN_STYLE,
  pythonRunStatus,
  type PythonValidationState,
} from '@/components/ui/PythonFunctionEditor'

/** 工作台下半区的固定面板：业务返回值在左，代码问题在右。 */
export function PythonRunPanel({
  kind,
  open,
  onToggle,
  running,
  result,
  stale,
  resultContent,
  onRevealError,
  className,
  title,
  hideTitle,
  headerExtra,
  statusOverride,
  contentOverride,
}: {
  kind: 'result' | 'problems'
  open: boolean
  onToggle: () => void
  running: boolean
  result: PythonValidationState | null
  /** 代码在最后一次试跑后又改过：保留内容，但不冒充当前结论。 */
  stale: boolean
  resultContent?: ReactNode
  onRevealError?: (line: number) => void
  className?: string
  /** 覆盖默认标题（「试跑结果」/「问题」）：双来源面板（如 生效/试跑 分段）改名用。 */
  title?: string
  hideTitle?: boolean
  /** 标题按钮与状态行之间的头部插槽（如 Segmented 分段）。 */
  headerExtra?: ReactNode
  /** 覆盖头部右侧状态行：内容语义不由试跑状态机描述时（如生效快照的新旧）。 */
  statusOverride?: ReactNode
  /** 整体覆盖正文：调用方自带内容结构时跳过内置的试跑结果/问题分支。 */
  contentOverride?: ReactNode
}) {
  const status = pythonRunStatus(running, result, stale)
  const style = PYTHON_RUN_STYLE[status]
  const failed = result != null && !result.valid
  const panelTitle = title ?? (kind === 'result' ? '试跑结果' : '问题')

  return (
    <WorkbenchPanel
      title={panelTitle}
      hideTitle={hideTitle}
      open={open}
      onToggle={onToggle}
      className={className}
      headerExtra={headerExtra}
      stale={stale}
      status={statusOverride ?? (
        <span className="ml-auto flex items-center gap-1.5 px-3.5 text-[12.5px]">
          {status === 'pass' && kind === 'result' ? (
            <Check size={13} className="text-accent" />
          ) : failed && kind === 'problems' ? (
            <TriangleAlert size={13} className="text-warn" />
          ) : (
            <Circle size={7} className={running ? 'fill-accent text-accent' : 'fill-ink-3 text-ink-3'} />
          )}
          <InkRewrite
            text={failed ? (kind === 'problems' ? '1 个问题' : '无返回值') : style.body}
            tone="label"
            textClassName={failed && kind === 'problems' ? 'text-warn' : style.text}
          />
        </span>
      )}
    >
        {contentOverride ?? (
        kind === 'result' ? (
          result?.valid ? (
            (resultContent ?? <p className="text-[13.5px] text-ink-2">函数执行成功。</p>)
          ) : failed ? (
            <p className="text-[13.5px] text-ink-3">执行失败，没有返回结果。请查看问题面板。</p>
          ) : (
            <p className="text-[13.5px] text-ink-3">试跑后在这里显示目标权重。</p>
          )
        ) : failed ? (
          <div>
            {result.errorLine != null && <button type="button" className="text-[12px] text-accent" onClick={() => onRevealError?.(result.errorLine!)}>定位到第 {result.errorLine} 行</button>}
            <PythonRunResultBody result={result} stale={stale} />
          </div>
        ) : (
          <p className="text-[13.5px] text-ink-3">
            {result?.valid ? '未发现执行问题。' : '语法错误和运行异常会显示在这里。'}
          </p>
        )
        )}
    </WorkbenchPanel>
  )
}
