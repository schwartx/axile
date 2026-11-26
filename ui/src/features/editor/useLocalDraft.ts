import { useEffect, useRef, useState } from 'react'
import type { PythonEditorKind } from '@/components/ui/pythonEditorContract'
import { canRestoreDraft, draftKey, readDrafts, writeDraft, type EditorDraft } from './drafts'

const DRAFT_TITLE: Record<PythonEditorKind, string> = {
  portfolio: '组合函数',
  account_notification: '账户通知函数',
  system_notification: '系统通知函数',
  signal_quotes: '行情函数',
  signal_account: '账户函数',
}

export function useLocalDraft(kind: PythonEditorKind, code: string, baseline: string, onChange: (code: string) => void, keyOverride?: string) {
  const path = window.location.pathname
  const key = keyOverride ?? draftKey(kind, path)
  const [loadedKey, setLoadedKey] = useState<string | null>(null)
  const [status, setStatus] = useState('读取本地草稿…')
  const [error, setError] = useState<Error | null>(null)
  const [recovery, setRecovery] = useState<EditorDraft | null>(null)
  const latest = useRef({ code, baseline, onChange })
  latest.current = { code, baseline, onChange }
  const [session, setSession] = useState<{ metadata: Pick<EditorDraft, 'key' | 'kind' | 'path' | 'title'>; revision: number | null; queue: Promise<void>; failed: boolean; saved?: Pick<EditorDraft, 'code' | 'baseline'> } | null>(null)

  useEffect(() => {
    let alive = true
    const initial = latest.current
    setLoadedKey(null)
    setSession(null)
    setError(null)
    setRecovery(null)
    void readDrafts().then(drafts => {
      if (!alive) return
      const stored = drafts.find(draft => draft.key === key)
      const metadata = stored?.kind === kind ? { key, kind, path: stored.path, title: stored.title }
        : { key, kind, path, title: DRAFT_TITLE[kind] }
      setSession({ metadata, revision: stored?.revision ?? null, queue: Promise.resolve(), failed: false, saved: stored })
      if (stored && stored.code !== initial.code && stored.code !== stored.baseline) {
        if (canRestoreDraft(stored, initial.baseline) && latest.current.code === initial.code) initial.onChange(stored.code)
        else {
          setRecovery(stored)
          setStatus('存在未恢复的本地草稿')
          return
        }
      }
      setLoadedKey(key)
    }).catch(cause => { if (alive) setError(cause instanceof Error ? cause : new Error(String(cause))) })
    return () => { alive = false }
  }, [key, kind, path])

  useEffect(() => {
    if (loadedKey !== key || error || !session || session.metadata.key !== key) return
    if (session.saved?.code === code && session.saved.baseline === baseline) { setStatus('已保存到本地'); return }
    let alive = true
    setStatus('保存到本地…')
    session.queue = session.queue.then(async () => {
      if (session.failed) throw new Error('本地草稿保存已停止，请先下载当前代码')
      const saved = await writeDraft({ ...session.metadata, code, baseline }, session.revision)
      session.revision = saved.revision
      session.saved = saved
      if (alive) setStatus('已保存到本地')
    }).catch(cause => { session.failed = true; if (alive) setError(cause instanceof Error ? cause : new Error(String(cause))) })
    return () => { alive = false }
  }, [loadedKey, key, code, baseline, error, session])

  useEffect(() => {
    if (code === baseline || session?.saved?.code === code) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [code, baseline, session, status])

  const restore = () => {
    if (!recovery) return
    latest.current.onChange(recovery.code)
    setRecovery(null)
    setLoadedKey(key)
  }
  const keepCurrent = () => { setRecovery(null); setLoadedKey(key) }
  return { key, status, error, recovery, restore, keepCurrent }
}
