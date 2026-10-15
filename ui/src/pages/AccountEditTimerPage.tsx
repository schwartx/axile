/**
 * 账户定时子页 /accounts/:id/edit/timer。
 *
 * 完整 :component:`TimerEditor`；标题与当前配置经 ``leading`` 进入左列，预览从标题通高。
 * 保存只 PATCH ``cron_expr``，保存与取消都不离开本页。
 */

import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router'
import { getAccount, updateAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import { AccountPageTitle } from '@/features/account/pageHead'
import {
  cronExprEqual,
  describeCron,
  parseSavedTimer,
  timerSupplement,
  timerStateToCronExpr,
  type TimerEditorState,
} from '@/features/setup/cron'
import { TimerEditor } from '@/features/setup/TimerEditor'
import { timerEditorError } from '@/features/setup/cron'
import { useChannelDescriptor } from '@/stores/channels'
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
  const account = usePolling(useCallback((s: AbortSignal) => getAccount(accountId, s), [accountId]), {
    queryKey: `account:${accountId}`,
    intervalMs: 0,
  })
  const acc = account.data
  const cachedAccount = accounts?.find((item) => item.account_id === accountId) ?? null
  const descriptor = useChannelDescriptor(acc?.trade_channel)
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

  const [ready, setReady] = useState(false)
  const [timer, setTimer] = useState<TimerEditorState | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<Error | null>(null)

  /** 草稿重置为服务端当前节奏；数据未就绪时返回 false（供首帧初始化门控）。 */
  const resetTimer = useCallback(() => {
    if (!acc || !descriptor) return false
    setTimer(parseSavedTimer(descriptor.schedule.kind, acc.cron_expr ?? '', descriptor.schedule.night, acc.supplement))
    setSaveError(null)
    return true
  }, [acc, descriptor])

  useEffect(() => {
    if (!ready && resetTimer()) setReady(true)
  }, [ready, resetTimer])

  if (account.error && !acc)
    return <EditError error={account.error} onRetry={account.refresh} />

  if (account.loading || !ready || !acc || !timer || !descriptor)
    return (
      <section className="pb-24">
        {title}
        <EditLoading bare />
      </section>
    )

  const scheduleKind = descriptor.schedule.kind
  const cronNext = timerStateToCronExpr(scheduleKind, timer, descriptor.schedule.night)
  const cronPrev = acc.cron_expr ?? ''
  const supplement = timerSupplement(timer)
  const dirty = !cronExprEqual(cronNext, cronPrev) || JSON.stringify(supplement) !== JSON.stringify(acc.supplement ?? null)
  const err = timerEditorError(timer)
  const changes = dirty ? [cronNext ? '定时任务已改' : '关闭自动调仓节奏'] : []

  // 与总览入口同构摘要（随草稿变）。
  const timerSummary = !cronNext.trim()
    ? '已关 · 仅手动'
    : (describeCron(scheduleKind, cronNext, descriptor.schedule.night) ?? '自定义执行节奏')

  const save = async () => {
    if (err) return toast(`执行节奏有误：${err}`)
    if (!dirty) return toast('没有改动')
    setSaving(true)
    setSaveError(null)
    try {
      await updateAccount(accountId, { cron_expr: cronNext, supplement })
      toast(cronNext ? '节奏已更新' : '已关闭自动调仓节奏')
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
  // EditSaveBar 自己按实际高度占位，窄视口保持自然流。
  return (
    <section className="min-[1120px]:flex min-[1120px]:h-full min-[1120px]:flex-col">
      <div className="min-[1120px]:min-h-0 min-[1120px]:flex-1">
        <TimerEditor
          tradeChannel={acc.trade_channel}
          scheduleKind={scheduleKind}
          nightSchedule={descriptor.schedule.night}
          value={timer}
          layout="page"
          leading={(
            <>
              {title}
              <EditSynopsis note="时间均为北京时间；保存只更新自动执行计划，不改启停状态。">
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
