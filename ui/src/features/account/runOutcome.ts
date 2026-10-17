import { executionOutcome } from '@/features/account/executionOutcome'

export type RunKind = 'exec' | 'clear'

export type RunOutcome =
  | { kind: 'success'; toast: string }
  | { kind: 'blocked'; toast: string }
  | { kind: 'not_reached' | 'unknown'; toast: string }
  | { kind: 'terminated'; toast: string }
  | { kind: 'failed'; error: string }

/**
 * 把明确的执行结论翻成 toast 意图。
 *
 * 不依赖用于调度的任务状态或输出状态。
 * ``failed`` 不弹 toast：账户状态行 / 近期执行已经承接同一次结果。
 */
export function describeRunOutcome(
  kind: RunKind,
  conclusion?: { status?: string; output_status?: string | null; error?: string | null },
): RunOutcome {
  const view = executionOutcome({ status: conclusion?.output_status ?? conclusion?.status, task_status: conclusion?.status, error: conclusion?.error }, kind === 'clear')
  if (view.state === 'FAILED') return { kind: 'failed', error: view.text }
  if (view.state === 'SUCCEEDED' || view.state === 'NOOP') return { kind: 'success', toast: view.text }
  if (view.state === 'BLOCKED') return { kind: 'blocked', toast: view.text }
  if (view.state === 'TERMINATED') return { kind: 'terminated', toast: view.text }
  if (view.state === 'PARTIAL') return { kind: 'not_reached', toast: view.text }
  return { kind: 'unknown', toast: view.text }
}

/** 终态结束轮询；部分完成仍保留未完成结果。 */
export function isTerminalExecutionTaskStatus(status: string): boolean {
  return ['SUCCEEDED', 'FAILED', 'TERMINATED', 'SKIPPED', 'PARTIAL'].includes(status)
}
