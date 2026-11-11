import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { usePythonDraft, usePythonSave } from '@/components/ui/pythonWorkbenchState'
import { Row, Section, TEXT } from '@/features/account/editUi'
import { saveBrowserTitle } from '@/lib/api/init'
import { browserTitle } from '@/lib/browserTitle'
import { useToastStore } from '@/stores/ui'

export function BrowserTitleSetting({ initialTitle }: { initialTitle: string }) {
  const state = usePythonDraft(initialTitle)
  const saveState = usePythonSave()
  const toast = useToastStore(s => s.toast)
  const dirty = state.draft.trim() !== state.baseline
  const save = () => saveState.save(dirty, async () => {
    const title = state.draft.trim()
    const result = await saveBrowserTitle(title)
    if (!result.ok) throw new Error(result.message)
    state.setDraft(title)
    state.setBaseline(title)
    document.title = browserTitle(title, window.location)
    toast(result.message)
  })

  return (
    <Section label="界面">
      <Row label="浏览器标签标题" hint="立即生效" top span>
        <form onSubmit={event => { event.preventDefault(); void save() }}>
          <div className="flex items-center gap-2">
            <input
              aria-label="浏览器标签标题"
              aria-describedby="browser-title-hint"
              className={`${TEXT} min-w-0 flex-1`}
              value={state.draft}
              maxLength={120}
              placeholder="留空使用默认标题"
              disabled={saveState.saving}
              onChange={event => { state.setDraft(event.target.value); saveState.clearError() }}
            />
            <button
              type="submit"
              className="flex-none cursor-pointer rounded-[9px] border border-line bg-surface px-4 py-2 text-[15px] text-ink-2 hover:border-ink-3/40 disabled:opacity-45"
              disabled={!dirty || saveState.saving}
            >
              <InkRewrite text={saveState.saving ? '保存中…' : '保存标题'} tone="label" />
            </button>
          </div>
          <p id="browser-title-hint" className="mt-1.5 text-[12px] leading-5 text-ink-3">
            为当前服务设置标题，所有浏览器共用。留空恢复默认标题；独立保存，无需重启。其他已打开的标签刷新后更新。
          </p>
          <ErrorNotice intent="mutation" title="保存标题失败" error={saveState.error} variant="mutation" onRetry={() => void save()} />
        </form>
      </Row>
    </Section>
  )
}
