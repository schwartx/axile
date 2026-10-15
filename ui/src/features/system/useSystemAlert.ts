/** 草稿只存内存；系统配置与函数工作台共享，跨页编辑不丢失待保存内容。 */
import { useEffect } from 'react'
import { create } from 'zustand'
import { usePythonSave } from '@/components/ui/pythonWorkbenchState'
import { initStatus, saveExecutionAlert } from '@/lib/api/init'
import { usePolling } from '@/lib/hooks/usePolling'
import { useToastStore } from '@/stores/ui'
import { systemAlertDraft, systemAlertEqual, systemAlertKey, type SystemAlertDraft } from './systemAlertDraft'

interface AlertState {
  draft: SystemAlertDraft | null
  baseline: SystemAlertDraft | null
  configured: boolean
  set: (patch: Partial<SystemAlertDraft>) => void
  restore: () => void
}
const useAlertStore = create<AlertState>((set) => ({
  draft: null, baseline: null, configured: false,
  set: (patch) => set((state) => ({ draft: state.draft ? { ...state.draft, ...patch } : null })),
  restore: () => set((state) => ({ draft: state.baseline })),
}))

export function useSystemAlert() {
  const state = useAlertStore()
  const query = usePolling(initStatus, { queryKey: 'system-alert', intervalMs: 0 })
  const saveState = usePythonSave()
  const toast = useToastStore((s) => s.toast)
  useEffect(() => {
    if (!query.data) return
    const { values } = query.data
    const next = systemAlertDraft(values)
    useAlertStore.setState((current) => ({
      baseline: next,
      draft: current.draft && current.baseline && !systemAlertEqual(current.draft, current.baseline) ? current.draft : next,
      configured: values.exe_err_feishu_configured,
    }))
  }, [query.data])
  const loading = query.loading || query.refreshing
  const dirty = Boolean(state.draft && state.baseline && !systemAlertEqual(state.draft, state.baseline))
  const blocked = state.draft?.mode === 'function' && !state.draft.code.trim()
  const save = (allowed = true) => saveState.save(dirty && !blocked && allowed && !loading, async () => {
    const submitted = useAlertStore.getState().draft!
    const key = systemAlertKey(submitted)
    const result = await saveExecutionAlert(key, submitted.mode, submitted.code)
    if (!result.ok) throw new Error(result.message)
    const baseline = { ...submitted, keyInput: '', clearKey: false }
    useAlertStore.setState((current) => ({
      baseline,
      draft: current.draft && systemAlertEqual(current.draft, submitted) ? baseline : current.draft,
      configured: key === null ? current.configured : Boolean(key),
    }))
    toast('系统告警配置已保存并立即生效')
  })
  return { ...state, loadError: query.error, loading, reload: query.refresh, dirty, blocked, saveState, save }
}
