import { useCallback, useEffect, useRef, useState } from 'react'
import { terminateExecution, type ExecutionTerminateMode } from '@/lib/api/executions'
import { shortErrorReason } from '@/lib/errorInfo'
import { useToastStore } from '@/stores/ui'

/**
 * 终止执行动作 + 防连点守卫。
 *
 * 点下即乐观 `terminating`（按钮切「终止中…」并禁用），直到执行真正离开运行态
 * （`isRunning` 落 false：TERMINATED/SUCCEEDED/FAILED 或 live 消失）才复位。
 *
 * 后端对重复 terminate 已幂等（不重复取消、不重复写审计），前端守卫只为免发废请求
 * 并给诚实反馈——正确性锚点在后端，多标签页/重试/直连 API 均绕不过它。
 *
 * 复位刻意挂 `isRunning` 而非 `useExecutionRunner` 的终态轮询：定时/其他来源发起的
 * 执行由服务端 live 驱动，runner 并不轮询，唯有 `isRunning` 能覆盖全部来源。
 *
 * Parameters
 * ----------
 * accountId : number
 *     目标账户 ID。
 * isRunning : boolean
 *     当前是否有执行在途（`live || runner.running` 的派生值）。
 * onRequested : (() => void) | undefined
 *     终止请求受理成功后的回调（如刷新记录列表）。
 *
 * Returns
 * -------
 * { terminating: boolean; terminate: (mode: ExecutionTerminateMode) => Promise<void> }
 *     `terminating` 驱动按钮 pending 态；`terminate` 为带防连点守卫的触发函数。
 */
export function useTerminateAction(accountId: number, isRunning: boolean, onRequested?: () => void) {
  const toast = useToastStore((s) => s.toast)
  const [terminating, setTerminating] = useState(false)
  // 防连点：乐观态已进 terminating 时忽略后续点击（state 闭包会旧，用 ref 立即挡）。
  const pendingRef = useRef(false)
  // 回调用 ref 存最新引用，避免其身份变化重建 terminate（同 useExecutionRunner.settledRef）。
  const requestedRef = useRef(onRequested)
  requestedRef.current = onRequested

  // 执行离开运行态即复位（与 runner 终态轮询解耦：live 驱动的执行也能落位）。
  useEffect(() => {
    if (!isRunning && pendingRef.current) {
      pendingRef.current = false
      setTerminating(false)
    }
  }, [isRunning])

  const terminate = useCallback(async (mode: ExecutionTerminateMode) => {
    if (pendingRef.current) return
    pendingRef.current = true
    setTerminating(true)
    try {
      const result = await terminateExecution(accountId, { mode })
      toast(result.message)
      requestedRef.current?.()
    } catch (err) {
      // 请求失败回滚 pending，允许重试（后端幂等，重试无副作用）。
      pendingRef.current = false
      setTerminating(false)
      toast(shortErrorReason(err))
    }
  }, [accountId, toast])

  return { terminating, terminate }
}
