import { useEffect, useState } from 'react'
import { Clock, TriangleAlert } from 'lucide-react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { ConfirmModal, type ConfirmSpec } from '@/components/ui/ConfirmModal'
import { Tooltip } from '@/components/ui/Tooltip'
import { cronError, cronToExpr, parseSavedTimer, resolveCronList, type TimerEditorState } from '@/features/setup/cron'
import { MOTION_LAYOUT } from '@/lib/viewTransition'
import { runTimeCheck, saveTimeCheck, type TimeCheckSettings } from '@/lib/api/timeCheck'
import { useTimeCheck } from '@/stores/timeCheck'
import { TimeCheckResult } from './TimeCheckResult'
import { TimeCheckSettingsPanel } from './TimeCheckSettingsPanel'

const primaryButton = 'inline-flex h-10 min-w-28 flex-none cursor-pointer items-center justify-center gap-2 rounded-[9px] border-0 bg-ink-1 px-5 text-[14.5px] font-semibold text-surface transition-opacity hover:opacity-85 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-45'
const secondaryButton = 'inline-flex h-10 flex-none cursor-pointer items-center justify-center gap-2 rounded-[9px] px-3 text-[14px] text-ink-2 transition-colors hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default disabled:opacity-45'
const date = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'

/** 顶栏入口和弹窗共享唯一服务状态；打开弹窗不触发探测。 */
export function TimeCheckButton() {
  const { snapshot, connected, error, refresh } = useTimeCheck()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<TimeCheckSettings | null>(null)
  const [timer, setTimer] = useState<TimerEditorState | null>(null)
  const [baseline, setBaseline] = useState('')
  const [pending, setPending] = useState<'save' | 'run' | null>(null)
  const busy = pending !== null
  const [actionError, setActionError] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null)
  const cron = timer ? cronToExpr(resolveCronList('continuous', { ...timer, supN: 0 })) : ''
  const rawError = timer?.timerTab === 'custom' ? cronError(timer.rawCron) : null
  const settings = draft ? { ...draft, cron_expr: cron } : null
  const dirty = !!settings && JSON.stringify(settings) !== baseline
  const running = !!snapshot?.state.running
  const alerts = Object.values(snapshot?.state.alerts ?? {})
  const state = snapshot?.status ?? 'disabled'
  const title = !snapshot ? '系统时间检查 · 状态尚未读取' : state === 'warning' ? `系统时间检查告警 · ${alerts.map(a => a.message).join('；')}`
    : state === 'enabled' ? `自动时间检查已开启 · 下次 ${date(snapshot.next_run_at)}` : '自动时间检查已关闭 · 点击手动检查'

  const loadDraft = () => {
    const config = useTimeCheck.getState().snapshot?.settings
    if (!config) return
    setActionError(null)
    setDraft({ ...config })
    setTimer(parseSavedTimer('continuous', config.cron_expr, null))
    setBaseline(JSON.stringify(config))
  }
  useEffect(() => {
    if (open && !draft && snapshot) loadDraft()
  }, [open, draft, snapshot])


  const show = () => {
    setDraft(null); setTimer(null); setActionError(null); setOpen(true)
    void refresh().catch(() => {})
  }
  const close = () => {
    if (busy) return
    if (dirty) {
      setConfirm({ title: '放弃未保存的设置？', body: '检查配置尚未保存。关闭将放弃本次修改，正在运行的检查会继续。', okText: '放弃修改', onConfirm: () => { setConfirm(null); setOpen(false) } })
    } else setOpen(false)
  }
  const save = async () => {
    if (!settings || rawError || !cron) return
    setPending('save'); setActionError(null)
    try {
      const saved = await saveTimeCheck(settings)
      useTimeCheck.getState().accept(saved)
      setDraft(saved.settings)
      setTimer(parseSavedTimer('continuous', saved.settings.cron_expr, null))
      setBaseline(JSON.stringify(saved.settings))
    } catch (error) { setActionError(error instanceof Error ? error.message : String(error)) }
    finally { setPending(null) }
  }
  const run = async () => {
    if (dirty || busy || running) return
    setPending('run'); setActionError(null)
    try { useTimeCheck.getState().accept(await runTimeCheck()) }
    catch (error) { setActionError(error instanceof Error ? error.message : String(error)) }
    finally { setPending(null) }
  }
  const invalid = !draft || !Number.isInteger(draft.threshold_ms) || draft.threshold_ms < 1 || draft.threshold_ms > 86400000
    || (draft.source === 'custom' && (!draft.custom_host.trim() || !Number.isInteger(draft.custom_port) || draft.custom_port < 1 || draft.custom_port > 65535))
  const conflict = draft && snapshot && draft.version !== snapshot.settings.version
  return <>
    <Tooltip content={title}>
      <button type="button" aria-label="系统时间检查" onClick={show} className={`relative grid h-8 w-8 cursor-pointer place-items-center rounded-chip hover:bg-fill ${state === 'warning' ? 'text-warn' : state === 'enabled' ? 'text-accent' : 'text-ink-3 hover:text-ink-1'}`}>
        <Clock size={17} aria-hidden />
        {state === 'warning' && <TriangleAlert size={10} aria-hidden className="absolute right-0 top-0 bg-surface" />}
      </button>
    </Tooltip>
    <CenteredModal open={open} title="系统时间检查"
      onClose={close} closeDisabled={busy || !!confirm} widthClassName="w-[960px] h-[min(720px,86dvh)]" scrollBody={false}
      footer={<div className="mt-1 flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-t border-line px-[22px] py-3.5">
        <div className="min-w-0 flex-1">
          <div inert={!dirty} aria-hidden={!dirty}
            className={`grid transition-[grid-template-rows] ${MOTION_LAYOUT} ${dirty ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
            <div className="min-h-0 overflow-hidden"><div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="mr-1 hidden text-xs text-ink-3 sm:inline">有未保存的修改</span>
              <button type="button" className={secondaryButton} disabled={busy} onClick={loadDraft}>取消修改</button>
              <button type="button" className={`${secondaryButton} font-semibold`} disabled={busy || invalid || !cron || !!rawError || !!conflict}
                onClick={() => void save()}>{pending === 'save' ? '保存中…' : '保存'}</button>
            </div></div>
          </div>
        </div>
        <div className="ml-auto flex flex-none items-center gap-3">
          {dirty && <span className="hidden text-xs text-ink-3 md:inline">请先保存修改</span>}
          <button type="button" className={primaryButton} disabled={!snapshot?.ready || busy || running || dirty} onClick={() => void run()}>
            <Clock size={15} aria-hidden />{pending === 'run' || running ? '检查中…' : '立即检查'}
          </button>
        </div>
      </div>}>
      <div className="flex min-h-0 flex-1 flex-col pb-2">
        {(error || actionError) && <p role="alert" className="mb-4 shrink-0 break-words text-sm text-warn">
          {actionError ?? error}<button type="button" className="ml-2 cursor-pointer underline" onClick={() => void refresh().catch(() => {})}>重新读取</button>
        </p>}
        {!connected && snapshot && <p className="mb-4 shrink-0 text-xs text-ink-3">实时状态连接中，当前显示最近读取的结果。</p>}
        <div className="grid min-h-0 flex-1 items-start gap-6 overflow-y-auto overscroll-contain md:grid-cols-[minmax(0,1fr)_minmax(0,1.65fr)] md:grid-rows-[minmax(0,1fr)] md:items-stretch md:gap-8 md:overflow-hidden">
          <div className="min-w-0 pb-2 md:min-h-0 md:overflow-y-auto md:overscroll-contain md:[scrollbar-gutter:stable]">
            <TimeCheckResult snapshot={snapshot} />
          </div>
          <section aria-label="检查设置" className="min-w-0 space-y-5 border-t border-line pb-2 pt-5 md:min-h-0 md:overflow-y-auto md:overscroll-contain md:border-t-0 md:border-l md:pt-0 md:pl-8 md:[scrollbar-gutter:stable]">
            {draft && timer ? <fieldset disabled={busy} className="min-w-0">
              <TimeCheckSettingsPanel
                key={baseline} draft={draft} onChange={setDraft} timer={timer} onTimerChange={setTimer}
                rawError={rawError}
                nextRun={snapshot?.settings.enabled ? date(snapshot.next_run_at) : '自动检查已关闭'}
                conflict={!!conflict} onReload={loadDraft} />
            </fieldset> : <p className="text-sm text-ink-3">正在读取设置…</p>}
          </section>
        </div>
      </div>
    </CenteredModal>
    <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />
  </>
}
