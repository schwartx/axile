import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { PythonWorkbench } from '@/components/ui/PythonWorkbench'
import { PythonWorkbenchPane } from '@/components/ui/PythonWorkbenchPane'
import type { PythonEditorHandle } from '@/components/ui/PythonFunctionEditor'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { readDrafts, downloadCode, type EditorDraft } from './drafts'
import { useOfflineEditor } from './offline'

function DraftWorkbench({ draft }: { draft: EditorDraft }) {
  const [code, setCode] = useState(draft.code)
  const [status, setStatus] = useState<HTMLDivElement | null>(null)
  const editor = useRef<PythonEditorHandle>(null)
  const offline = useOfflineEditor()
  return <PythonWorkbench storageKey="axile.localEditor.width" resizingPanels={false}
    saveAction={{ dirty: false, saving: false, blocked: true, blockedReason: '本地草稿 · 保存到服务器需联网', onSave: () => {} }}
    status={<div ref={setStatus} className="mr-2" />}
    inspector={<aside className="space-y-4 overflow-y-auto border-r border-line bg-surface p-4 text-[13px] text-ink-2">
      <a href="/editor" className="text-accent">所有本地草稿</a>
      <h1 className="text-[17px] font-semibold text-ink-1">{draft.title}</h1>
      <p>{draft.path}</p>
      <p role="status">{offline}</p>
      <p>补全、代码检查和格式化在本地运行。服务器试跑和提交请返回原页面操作。</p>
      <button type="button" className="text-accent" onClick={() => downloadCode(code)}>下载当前代码</button>
      <a href={draft.path} className="block text-accent">返回原页面</a>
    </aside>}>
    <PythonWorkbenchPane kind={draft.kind} code={code} savedCode={draft.baseline} draftKey={draft.key}
      onChange={setCode} running={false} runDisabled stale={false} result={null} onRun={() => {}}
      editorRef={editor} statusTarget={status} storageKey="axile.localEditor.split" title={draft.title} />
  </PythonWorkbench>
}

/** This route deliberately mounts outside account polling and the business event stream. */
export function LocalEditorPage() {
  const [params] = useSearchParams()
  const [drafts, setDrafts] = useState<EditorDraft[] | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const offline = useOfflineEditor()
  useEffect(() => { void readDrafts().then(setDrafts).catch(cause => setError(cause instanceof Error ? cause : new Error(String(cause)))) }, [])
  const selected = params.get('draft') ?? drafts?.find(item => item.path === window.location.pathname)?.key
  const draft = drafts?.find(item => item.key === selected)
  return <main className="h-dvh bg-canvas text-ink-1">
    {draft ? <DraftWorkbench key={draft.key} draft={draft} /> : <div className="mx-auto max-w-3xl space-y-5 p-6">
      <h1 className="text-[22px] font-semibold">本地 Python 草稿</h1>
      <p role="status" className="text-[13px] text-ink-3">{offline}</p>
      <ErrorNotice intent="read" title="本地草稿读取失败" error={error} />
      {drafts === null && !error && <p>读取草稿…</p>}
      {drafts?.length === 0 && <p>在线打开组合或通知编辑器后，代码会保存到这里。</p>}
      {selected && drafts && !draft && <p role="alert" className="text-warn">此浏览器中没有对应的草稿。</p>}
      <ul className="divide-y divide-line">{drafts?.map(item => <li key={item.key} className="py-3">
        <a href={`/editor?draft=${encodeURIComponent(item.key)}`} className="text-accent">{item.title} · {item.path}</a>
        <p className="mt-1 text-[12px] text-ink-3">{new Date(item.updatedAt).toLocaleString()}</p>
      </li>)}</ul>
      <a href="/" className="text-[13px] text-accent">返回 Axile</a>
    </div>}
  </main>
}
