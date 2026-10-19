/**
 * 账户详情「自动执行」轻量节奏弹层（方案 B）。
 *
 * 只做高频：总开关 + 快捷预设 + 补发 + 下次预览。高级 / 多规则 / 裸 cron 链到编辑页。
 * 壳与 :component:`ImportModal` 同级（居中 dialog + scrim），不进完整 :component:`TimerEditor`。
 */

import { useEffect, useState } from 'react'
import { Check } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { SupplementControls } from '@/features/setup/SupplementControls'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { MOTION_LAYOUT, useRemountFade } from '@/lib/viewTransition'
import {
  DEFAULT_PRESET,
  PRESETS,
  cronExprEqual,
  defaultTimerEditorState,
  fmtFire,
  nextFires,
  parseSavedTimer,
  timerSupplement,
  type Supplement,
  resolveCronList,
  ruleFromPreset,
  timerStateToCronExpr,
  type NightSchedule,
  type ScheduleKind,
  type TimerEditorState,
} from '@/features/setup/cron'
import { updateAccount } from '@/lib/api/accounts'
import { useToastStore } from '@/stores/ui'
import { useChannelDescriptor } from '@/stores/channels'

function Switch({ on, ariaLabel, onClick }: { on: boolean; ariaLabel: string; onClick: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-label={ariaLabel}
      aria-checked={on}
      onClick={onClick}
      className={`relative h-[27px] w-[46px] flex-none rounded-full transition-colors ${MOTION_LAYOUT} ${
        on ? 'bg-accent' : 'bg-border-strong'
      }`}
    >
      <span
        className={`absolute top-[3px] h-[21px] w-[21px] rounded-full bg-surface shadow transition-all ${MOTION_LAYOUT} ${
          on ? 'left-[22px]' : 'left-[3px]'
        }`}
      />
    </button>
  )
}

/**
 * 打开弹层时的草稿：能落在快捷则原样；高级/自定义则降级为快捷默认，并标 ``fromComplex``。
 */
function draftFromCron(
  market: ScheduleKind,
  cronExpr: string,
  night?: NightSchedule | null,
  supplement?: Supplement | null,
): { state: TimerEditorState; fromComplex: boolean } {
  const parsed = parseSavedTimer(market, cronExpr, night, supplement)
  const quick = parsed.timerTab === 'quick'
  if (quick) return { state: { ...parsed, timerTab: 'quick', rawCron: '' }, fromComplex: false }

  // 有表达式但非快捷：保留开关，预设回到市场默认，避免误把高级时刻当已选中。
  const base = defaultTimerEditorState(market)
  return {
    state: {
      ...base,
      autoOn: parsed.autoOn,
      timerTab: 'quick',
      rawCron: '',
    },
    fromComplex: parsed.autoOn,
  }
}

/** 编译时强制快捷意图（弹层不允许自定义 / 高级编译路径）。 */
function asQuickIntent(market: ScheduleKind, s: TimerEditorState): TimerEditorState {
  const id = s.presetIds[0] ?? DEFAULT_PRESET[market]
  const rule = ruleFromPreset(market, id)
  return {
    ...s,
    timerTab: 'quick',
    rawCron: '',
    presetIds: [id],
    scheduleRules: [rule],
    selectedRuleId: rule.id,
  }
}

export interface TimerQuickModalProps {
  open: boolean
  accountId: number
  accountName: string
  tradeChannel: string
  /** 当前存储的 cron_expr。 */
  cronExpr: string
  supplement?: Supplement | null
  onClose: () => void
  /** 保存成功后刷新详情 / next_run / 仪表盘。 */
  onSaved: () => void
}

/**
 * 轻量定时弹层。
 */
function TimerQuickModalReady({
  open,
  accountId,
  accountName,
  scheduleKind,
  nightSchedule,
  cronExpr,
  supplement,
  onClose,
  onSaved,
}: TimerQuickModalProps & { scheduleKind: ScheduleKind; nightSchedule?: NightSchedule | null }) {
  const toast = useToastStore((s) => s.toast)
  const market = scheduleKind
  const [state, setState] = useState<TimerEditorState>(() => draftFromCron(market, cronExpr, nightSchedule, supplement).state)
  // 开关切到「开」的那一帧才播入场；初次以开挂载不播（首帧就位）。
  const autoOnFade = useRemountFade(state.autoOn)
  const [fromComplex, setFromComplex] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<Error | null>(null)

  useEffect(() => {
    if (!open) return
    const d = draftFromCron(market, cronExpr, nightSchedule, supplement)
    setState(d.state)
    setFromComplex(d.fromComplex)
    setSaving(false)
    setSaveError(null)
  }, [open, market, cronExpr, nightSchedule, supplement])


  const patch = (p: Partial<TimerEditorState>) => {
    setSaveError(null)
    setState((prev) => ({ ...prev, ...p }))
  }

  const pickPreset = (id: string) => {
    const rule = ruleFromPreset(market, id)
    patch({
      presetIds: [id],
      scheduleRules: [rule],
      selectedRuleId: rule.id,
      timerTab: 'quick',
      rawCron: '',
    })
    setFromComplex(false)
  }

  const intent = asQuickIntent(market, state)
  const cronNext = fromComplex && state.autoOn ? cronExpr : timerStateToCronExpr(market, intent, nightSchedule)
  const nextSupplement = timerSupplement(state)
  const dirty = !cronExprEqual(cronNext, cronExpr) || JSON.stringify(nextSupplement) !== JSON.stringify(supplement ?? null)
  const fires = state.autoOn ? nextFires(resolveCronList(market, { ...intent, supN: 0 }, nightSchedule), 4) : []

  const save = async () => {
    if (!dirty) {
      onClose()
      return
    }
    if (fromComplex && state.supN > 0) {
      toast('请先选择并确认基础节奏，再启用独立补发')
      return
    }
    setSaving(true)
    setSaveError(null)
    try {
      await updateAccount(accountId, { cron_expr: cronNext, supplement: nextSupplement })
      toast(cronNext ? '节奏已更新' : '已关闭自动调仓节奏')
      onSaved()
      onClose()
    } catch (e) {
      setSaveError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      setSaving(false)
    }
  }

  const presetCardT =
    'transition-[border-color,background-color,box-shadow] duration-200 ease-[cubic-bezier(0.4,0,0.2,1)] motion-reduce:transition-none'

  return (
    <CenteredModal
      open={open}
      title="定时任务"
      subtitle={`${accountName} · 快捷预设 · 时间均为北京时间`}
      ariaLabel="调整定时任务"
      onClose={onClose}
      closeDisabled={saving}
      footer={
        <div className="mt-1 flex justify-end gap-2.5 border-t border-line px-5 py-3.5">
          <button
            type="button"
            className="cursor-pointer rounded-[9px] border border-line bg-surface px-4 py-2 text-sm text-ink-2 disabled:opacity-45"
            onClick={onClose}
            disabled={saving}
          >
            取消
          </button>
          <button
            type="button"
            className="cursor-pointer rounded-[9px] border border-ink-1 bg-ink-1 px-[18px] py-2 text-sm font-[550] text-surface disabled:opacity-45"
            onClick={() => void save()}
            disabled={saving || !dirty}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      }
    >
      {fromComplex && (
        <div className="mb-4 rounded-[10px] border-l-2 border-warn/60 bg-warn-tint/50 py-2 pl-3 pr-2 text-[13.5px] text-ink-2">
          当前为高级或自定义节奏。在此保存将改为下方快捷预设；完整编辑请到账户编辑页。
        </div>
      )}

      <div className="space-y-5">
        <div className="flex items-center gap-3">
          <Switch ariaLabel="自动调仓" on={state.autoOn} onClick={() => patch({ autoOn: !state.autoOn })} />
          <div>
            <div className="text-sm font-[640]">{state.autoOn ? '自动调仓已开' : '自动调仓已关'}</div>
            <div className="text-xs text-ink-3">
              {state.autoOn ? '按下方节奏自动执行' : '仅手动 / 外接触发'}
            </div>
          </div>
        </div>

        <div
          className={`grid transition-[grid-template-rows,opacity] ${MOTION_LAYOUT} ${
            state.autoOn ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0'
          }`}
        >
          <div className="overflow-hidden">
            <div className={`space-y-4 ${state.autoOn && autoOnFade ? 'panel-fade-in' : ''}`}>
              <div>
                <div className="mb-2 text-sm font-[640]">节奏</div>
                <div className="flex flex-wrap gap-2">
                  {PRESETS[market].map((p) => {
                    const on = state.presetIds.includes(p.id)
                    return (
                      <button
                        key={p.id}
                        type="button"
                        aria-pressed={on}
                        onClick={() => pickPreset(p.id)}
                        className={`min-w-[92px] rounded-[11px] border p-2.5 text-left ${presetCardT} ${
                          on
                            ? 'border-accent bg-accent-soft shadow-[inset_0_0_0_1px_var(--color-accent)]'
                            : 'border-line bg-surface'
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2 text-sm font-[640]">
                          <span>{p.label}</span>
                          <Check size={13} aria-hidden="true" className={`shrink-0 ${on ? '' : 'invisible'}`} />
                        </div>
                        {p.sub && <div className="text-xs text-ink-3">{p.sub}</div>}
                      </button>
                    )
                  })}
                </div>
              </div>

              {nightSchedule && (
                <div className="flex items-center gap-3">
                  <Switch
                    ariaLabel={nightSchedule.label}
                    on={state.nightOn}
                    onClick={() => patch({ nightOn: !state.nightOn })}
                  />
                  <div className="min-w-0">
                    <div className="text-sm font-[640]">{nightSchedule.label}</div>
                    <div className="text-xs text-ink-3">{nightSchedule.range_label}</div>
                  </div>
                </div>
              )}

              <SupplementControls
                supN={state.supN}
                supM={state.supM}
                onN={(supN) => patch({ supN })}
                onM={(supM) => patch({ supM })}
              />

              <div>
                <div className="mb-1 text-sm font-[640]">
                  下次排程 <span className="text-xs font-normal text-ink-3">预览 · 北京时间</span>
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-1">
                  {fires.length ? (
                    fires.map((f, i) => (
                      <span key={i} className={`text-sm ${i === 0 ? 'font-[640]' : 'text-ink-3'}`}>
                        {fmtFire(f)}
                      </span>
                    ))
                  ) : (
                    <span className="text-sm text-ink-3">— 选择节奏后显示 —</span>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="border-t border-line pt-3">
          <Link
            to={`/accounts/${accountId}/edit/timer`}
            className="text-[13.5px] font-semibold text-accent hover:underline"
            onClick={onClose}
          >
            更多节奏 · 多时刻与自定义 →
          </Link>
        </div>
      </div>
      <ErrorNotice intent="mutation" embedded title="保存定时任务失败" error={saveError} variant="mutation" onRetry={save} />
    </CenteredModal>
  )
}

/** 渠道能力未加载时不猜测调度类型，也不挂载可编辑弹层。 */
export function TimerQuickModal(props: TimerQuickModalProps) {
  const schedule = useChannelDescriptor(props.tradeChannel)?.schedule
  return schedule
    ? <TimerQuickModalReady {...props} scheduleKind={schedule.kind} nightSchedule={schedule.night} />
    : null
}
