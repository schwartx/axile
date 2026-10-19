import { useCallback, useRef, useState } from 'react'
import type { PythonValidationState } from '@/components/ui/PythonFunctionEditor'

/** 草稿和已保存基线分开：异步保存只确认提交时的版本。 */
export function usePythonDraft<T>(initial: T, equal: (a: T, b: T) => boolean = Object.is) {
  const [draft, setDraft] = useState(initial)
  const [baseline, setBaseline] = useState(initial)
  return { draft, setDraft, baseline, setBaseline, dirty: !equal(draft, baseline), restore: () => setDraft(baseline) }
}

/** 同步门锁让连续快捷键也只能发出一次请求。 */
export function createPythonOperationGate() {
  let busy = false
  return async <T>(allowed: boolean, operation: () => Promise<T>): Promise<T | null> => {
    if (!allowed || busy) return null
    busy = true
    try {
      return await operation()
    } finally {
      busy = false
    }
  }
}

export function usePythonSave() {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const clearError = useCallback(() => setError(null), [])
  const gate = useRef(createPythonOperationGate()).current
  const save = async (allowed: boolean, operation: () => Promise<void>): Promise<void> => {
    await gate(allowed, async () => {
      setSaving(true)
      setError(null)
      try {
        await operation()
      } catch (cause) {
        setError(cause instanceof Error ? cause : new Error(String(cause)))
      } finally {
        setSaving(false)
      }
    })
  }
  return { saving, error, clearError, save }
}

export function pythonRunIsStale(result: unknown, ranInput: string | null, input: string) {
  return result != null && ranInput !== input
}

/** 页面提供请求和结果转换；运行、失败及旧结果的生命周期统一管理。 */
export function usePythonRun<T>({ code, contextKey = '', enabled = true, execute, failed, toEditorResult }: {
  code: string
  contextKey?: string
  enabled?: boolean
  execute: (code: string) => Promise<T>
  failed: (cause: unknown) => T
  toEditorResult: (result: T) => PythonValidationState
}) {
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<T | null>(null)
  const [editorResult, setEditorResult] = useState<PythonValidationState | null>(null)
  const [ranInput, setRanInput] = useState<string | null>(null)
  const gate = useRef(createPythonOperationGate()).current
  const input = JSON.stringify([code, contextKey])
  const canRun = enabled && Boolean(code.trim()) && !running
  const run = (executeOverride?: (code: string) => Promise<T>) => gate(canRun, async () => {
    setRunning(true)
    try {
      let next: T
      let failure: unknown = null
      try {
        next = await (executeOverride ?? execute)(code)
      } catch (cause) {
        failure = cause
        next = failed(cause)
      }
      setResult(next)
      // 只在新试跑完成时更新视图身份，普通编辑不重新展开结果面板。
      setEditorResult({ ...toEditorResult(next), ...(failure != null ? { error: failure } : {}) })
      setRanInput(input)
      return next
    } finally {
      setRunning(false)
    }
  })
  return { running, result, editorResult, stale: pythonRunIsStale(result, ranInput, input), canRun, run }
}
