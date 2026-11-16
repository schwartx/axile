import type { PythonEditorKind } from '@/components/ui/pythonEditorContract'

export interface EditorDraft {
  key: string
  kind: PythonEditorKind
  path: string
  title: string
  code: string
  baseline: string
  revision: number
  updatedAt: number
}

export function draftKey(kind: PythonEditorKind, path: string) {
  return `${kind}:${path}`
}

let database: Promise<IDBDatabase> | undefined
function openDatabase() {
  return database ??= new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('axile-editor', 1)
    request.onupgradeneeded = () => request.result.createObjectStore('drafts', { keyPath: 'key' })
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error ?? new Error('无法打开本地草稿存储'))
    request.onblocked = () => reject(new Error('本地草稿存储升级被其他页面阻止'))
  })
}

function validDraft(value: unknown): value is EditorDraft {
  if (!value || typeof value !== 'object') return false
  const draft = value as EditorDraft
  return ['portfolio', 'account_notification', 'system_notification'].includes(draft.kind)
    && ['key', 'path', 'title', 'code', 'baseline'].every(key => typeof draft[key as keyof EditorDraft] === 'string')
    && Number.isSafeInteger(draft.revision) && draft.revision > 0 && Number.isFinite(draft.updatedAt)
    && draft.path.startsWith('/') && !draft.path.startsWith('//')
}

export async function readDrafts(): Promise<EditorDraft[]> {
  const db = await openDatabase()
  return new Promise((resolve, reject) => {
    const request = db.transaction('drafts').objectStore('drafts').getAll()
    request.onsuccess = () => {
      if (!request.result.every(validDraft)) { reject(new Error('本地草稿数据损坏，未覆盖原记录')); return }
      resolve(request.result.sort((a: EditorDraft, b: EditorDraft) => b.updatedAt - a.updatedAt))
    }
    request.onerror = () => reject(request.error)
  })
}

/** Revision checks run inside one transaction, so another tab cannot silently replace a draft. */
export async function writeDraft(draft: Omit<EditorDraft, 'revision' | 'updatedAt'>, expectedRevision: number | null): Promise<EditorDraft> {
  if (new TextEncoder().encode(draft.code).length > 2 * 1024 * 1024) throw new Error('草稿超过 2 MiB，无法保存到本地')
  const db = await openDatabase()
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('drafts', 'readwrite')
    const store = transaction.objectStore('drafts')
    const read = store.get(draft.key)
    let result: EditorDraft
    let conflict: Error | null = null
    read.onsuccess = () => {
      const existing: unknown = read.result
      if ((existing !== undefined && !validDraft(existing)) || (existing as EditorDraft | undefined)?.revision !== (expectedRevision ?? undefined)) {
        conflict = new Error('本地草稿已被其他页面修改；当前代码仍保留，可下载后再处理')
        transaction.abort()
        return
      }
      result = { ...draft, revision: (expectedRevision ?? 0) + 1, updatedAt: Date.now() }
      store.put(result)
    }
    transaction.oncomplete = () => resolve(result)
    transaction.onabort = () => reject(conflict ?? transaction.error ?? new Error('本地草稿保存失败'))
    transaction.onerror = () => reject(transaction.error ?? new Error('本地草稿保存失败'))
  })
}

export function canRestoreDraft(draft: EditorDraft, baseline: string) {
  return draft.code !== draft.baseline && draft.baseline === baseline
}

export function downloadCode(code: string) {
  const url = URL.createObjectURL(new Blob([code], { type: 'text/x-python;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'portfolio.py'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
