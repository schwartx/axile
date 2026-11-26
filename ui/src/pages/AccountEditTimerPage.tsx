/**
 * 账户定时子页 /accounts/:id/edit/timer。
 *
 * 完整 :component:`TimerEditor`；标题与当前配置经 ``leading`` 进入左列，预览从标题通高。
 * 保存只 PATCH ``cron_expr``，保存与取消都不离开本页。
 */

import { useCallback, useEffect, useState } from 'react'
import { useOutletContext, useParams } from 'react-router'
import { updateAccount } from '@/lib/api/accounts'
import type { AccountEditContext } from '@/features/account/AccountEditGuard'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import { AccountPageTitle } from '@/features/account/pageHead'
import {
  cronExprEqual,
  gmTimerSessions,
  describeCron,
  parseSavedTimer,
  timerSupplement,
  timerStateToCronExpr,
  type TimerEditorState,
} from '@/features/setup/cron'
import { TimerEditor } from '@/features/setup/TimerEditor'
import { timerEditorError } from '@/features/setup/cron'
import { useChannelCatalogStore, useChannelDescriptor } from '@/stores/channels'
import { SIGNAL_CHANNEL } from '@/features/signal/signalData'
import { portfolioMarketDescriptor } from '@/features/portfolio/portfolioMarkets'
import {
  EditError,
  EditLoading,
  EditSaveBar,
  EditSynopsis,
} from '@/features/account/editUi'

export function AccountEditTimerPage() {
  const { id } = useParams()
  const accountId = Number(id)
  const toast = useToastStore((s) => s.toast)
  const accounts = useDomainStore((s) => s.accounts)
  const refreshAccounts = useDomainStore((s) => s.refreshAccounts)
  const account = useOutletContext<AccountEditContext>()
  const acc = account.data
  const cachedAccount = accounts?.find((item) => item.account_id === accountId) ?? null
  const channel = useChannelDescriptor(acc?.trade_channel)
  const channels = useChannelCatalogStore((state) => state.channels)
  const catalogError = useChannelCatalogStore((state) => state.error)
  const refreshCatalog = useChannelCatalogStore((state) => state.refresh)
  const signal = acc?.trade_channel === SIGNAL_CHANNEL
  const descriptor = signal ? portfolioMarketDescriptor(channels ?? [], acc.market) : channel
  const title = (
    <div className="flex flex-wrap items-center gap-3">
      <AccountPageTitle
        accountId={accountId}
        page="定时任务"
        name={acc?.name ?? cachedAccount?.name}
        channel={acc?.trade_channel ?? cachedAccount?.trade_channel}
        market={acc?.market ?? cachedAccount?.market}
      />
    </div>
  )

  const readTimer = useCallback(() => {
    if (!descriptor) return null
    const saved = parseSavedTimer(descriptor.schedule.kind, acc.cron_expr ?? '', descriptor.schedule.night,
      signal ? null : acc.supplement, acc.channel_options.gm)
    return signal ? { ...saved, autoOn: acc.is_started && saved.autoOn } : saved
  }, [acc, descriptor, signal])
  const [timer, setTimer] = useState(readTimer)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<Error | null>(null)
  const resetTimer = useCallback(() => {
    setTimer(readTimer())
    setSaveError(null)
  }, [readTimer])

  useEffect(() => {
    if (!timer && descriptor) resetTimer()
  }, [timer, descriptor, resetTimer])

  if (signal && !descriptor && (catalogError || channels))
    return <div className="px-4 pt-5 md:px-5"><EditError error={catalogError ?? new Error('所选市场未注册')} onRetry={refreshCatalog} /></div>

  if (!timer || !descriptor)
    return (
      <section className="px-4 pt-5 md:px-5">
        {title}
        <EditLoading bare />
      </section>
    )

  const scheduleKind = descriptor.schedule.kind
  const cronNext = timerStateToCronExpr(scheduleKind, timer, descriptor.schedule.night)
  const cronPrev = acc.cron_expr ?? ''
  const supplement = signal ? null : timerSupplement(timer)
  const gmSessions = gmTimerSessions(descriptor.channel, timer)
  const sessionsChanged = JSON.stringify(gmSessions) !== JSON.stringify(gmTimerSessions(descriptor.channel, { gm_sessions: acc.channel_options.gm }))
  const enabledChanged = signal && timer.autoOn !== (acc.is_started && Boolean(cronPrev))
  const cronChanged = (!signal || timer.autoOn) && !cronExprEqual(cronNext, cronPrev)
  const supplementChanged = !signal && JSON.stringify(supplement) !== JSON.stringify(acc.supplement ?? null)
  const dirty = enabledChanged || cronChanged || supplementChanged || sessionsChanged
  const err = timerEditorError(timer) || (signal && timer.autoOn && !cronNext ? '请至少设置一个计算时点' : null)
  const changes = dirty ? [cronNext ? '定时任务已改' : signal ? '关闭自动执行' : '关闭自动调仓节奏'] : []

  // 与总览入口同构摘要（随草稿变）。
  const timerSummary = !cronNext.trim()
    ? '已关 · 仅手动'
    : (describeCron(scheduleKind, cronNext, descriptor.schedule.night, gmSessions) ?? '自定义执行节奏')

  const save = async () => {
    if (err) return toast(`执行节奏有误：${err}`)
    if (!dirty) return toast('没有改动')
    setSaving(true)
    setSaveError(null)
    try {
      await updateAccount(accountId, {
        cron_expr: cronNext,
        ...(signal ? { is_started: timer.autoOn } : { supplement }),
        ...(sessionsChanged ? { channel_options: { ...acc.channel_options, ...(gmSessions ? { gm: gmSessions } : {}) } } : {}),
      })
      toast(enabledChanged ? timer.autoOn ? '已启动' : '已暂停' : cronNext ? '节奏已更新' : '已关闭自动调仓节奏')
      void refreshAccounts()
      account.refresh()
    } catch (e) {
      setSaveError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      setSaving(false)
    }
  }

  // 宽视口下撑起高度链（h-full → flex-1 min-h-0），让 TimerEditor 的预览右列通高；
  // 标题 / 当前配置经 leading 进左列，预览与标题顶对齐。
  // 此页不叠加外壳的底部留白，EditSaveBar 按实际高度占位；窄视口保持自然流。
  return (
    <section className="px-4 pt-5 md:px-5 min-[1120px]:flex min-[1120px]:h-full min-[1120px]:flex-col">
      <div className="min-[1120px]:min-h-0 min-[1120px]:flex-1">
        <TimerEditor
          tradeChannel={acc.trade_channel}
          scheduleChannel={descriptor.channel}
          scheduleKind={scheduleKind}
          nightSchedule={descriptor.schedule.night}
          value={timer}
          layout="page"
          leading={(
            <>
              {title}
              <EditSynopsis note={signal ? '时间均为北京时间。绑定组合后按计划执行，按通知设置推送信号。'
                : '时间均为北京时间；保存只更新自动执行计划，不改启停状态。'}>
                {timerSummary}
              </EditSynopsis>
            </>
          )}
          onChange={(next) =>
            { setSaveError(null); setTimer((prev) => (typeof next === 'function' ? next(prev as TimerEditorState) : next)) }
          }
        />
      </div>

      <EditSaveBar
        changes={changes}
        blocked={Boolean(err)}
        onCancel={resetTimer}
        onSave={() => void save()}
        saving={saving}
        error={saveError}
      />
    </section>
  )
}
