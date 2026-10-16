import { useCallback, useEffect, useRef, useState } from 'react'
import { getExecutionStatus, triggerEmptyPositions, triggerExecute } from '@/lib/api/executions'
import { describeRunOutcome, type RunKind } from '@/features/account/runOutcome'
import { useToastStore } from '@/stores/ui'
import type { ExecutionStatus } from '@/types/api'
import { useReadRecovery } from '@/lib/hooks/useReadRecovery'

const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'TERMINATED', 'SKIPPED'])
export type { RunKind }
interface RunnerState {
  running: boolean
  kind: RunKind | null
  executionId: string | null
  lastStatus: ExecutionStatus | null
  queryError: Error | null
  triggerError: Error | null
}
const initial: RunnerState = { running: false, kind: null, executionId: null, lastStatus: null, queryError: null, triggerError: null }

/** 串行读取同一执行；查询失败不等于交易终态，也不解除重复执行约束。 */
export function useExecutionRunner(accountId: number, onSettled?: () => void) {
  const toast = useToastStore((s) => s.toast)
  const [state, setState] = useState<RunnerState>(initial)
  const [attempt, setAttempt] = useState(0)
  const runningRef = useRef(false)
  const generation = useRef(0)
  const settledRef = useRef(onSettled)
  settledRef.current = onSettled
  useEffect(() => {
    setState(initial)
    runningRef.current = false
    return () => { generation.current += 1 }
  }, [accountId])

  useEffect(() => {
    if (!state.running || !state.executionId || !state.kind) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const executionId = state.executionId
    const kind = state.kind
    const poll = async () => {
      try {
        const status = await getExecutionStatus(executionId, controller.signal)
        if (controller.signal.aborted) return
        const terminal = TERMINAL.has(status.status)
        setState(previous => ({ ...previous, lastStatus: status, queryError: null, running: !terminal }))
        if (terminal) {
          runningRef.current = false
          const outcome = describeRunOutcome(kind, status)
          if (outcome.kind !== 'failed') toast(outcome.toast)
          settledRef.current?.()
        } else timer = setTimeout(() => void poll(), 1500)
      } catch (cause) {
        if (!controller.signal.aborted) setState(previous => ({ ...previous, queryError: cause instanceof Error ? cause : new Error(String(cause)) }))
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [accountId, state.running, state.executionId, state.kind, attempt, toast])

  const start = useCallback(async (kind: RunKind) => {
    if (runningRef.current) return
    runningRef.current = true
    const request = ++generation.current
    setState({ ...initial, running: true, kind })
    try {
      const trigger = await (kind === 'exec' ? triggerExecute : triggerEmptyPositions)(accountId)
      if (request !== generation.current) return
      setState({ ...initial, running: true, kind, executionId: trigger.execution_id })
      toast(trigger.accepted === 'coalesced' ? '已与等待中的执行合并' : kind === 'exec' ? '已触发执行…' : '已触发清仓…')
    } catch (cause) {
      if (request !== generation.current) return
      runningRef.current = false
      setState({ ...initial, kind, triggerError: cause instanceof Error ? cause : new Error(String(cause)) })
    }
  }, [accountId, toast])

  useReadRecovery(() => setAttempt(value => value + 1), state.running && state.executionId != null && state.queryError != null)
  return { ...state, start, resumeQuery: () => setAttempt(value => value + 1) }
}
