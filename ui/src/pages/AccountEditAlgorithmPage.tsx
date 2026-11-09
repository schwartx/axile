import { shortErrorReason } from '@/lib/errorInfo'
/**
 * 账户执行算法子页 /accounts/:id/edit/algorithm。
 *
 * 主交易 + 清仓算法完整编辑器；保存只 PATCH 改动字段，保存与取消都不离开本页。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Info } from 'lucide-react'
import { Tooltip } from '@/components/ui/Tooltip'
import { ReplayRecordingStatus } from '@/features/replay/ReplayRecordingStatus'
import { replayRecordingPatch } from '@/features/replay/accountSetting'
import { useParams, useViewTransitionState } from 'react-router'
import { getAccount, updateAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import { AccountPageTitle } from '@/features/account/pageHead'
import { useChannelDescriptor } from '@/stores/channels'
import {
  accountConfigVtName,
  writeAccountConfigSummary,
} from '@/features/account/configSummary'
import { algorithmTransitionSlot, readAlgorithmAccountPreview, writeAlgorithmAccountPreview } from '@/features/account/algorithmNameTransition'
import {
  algorithmRefOf,
  describeAlgorithmRef,
  validateAlgorithmRef,
  type AlgorithmRef,
} from '@/features/setup/algorithms'
import { FuturesClosePolicyEditor } from '@/features/account/FuturesClosePolicyEditor'
import { defaultClosePolicy } from '@/features/account/futuresClosePolicy'
import { RuntimeSyncStatus } from '@/features/account/RuntimeSyncStatus'
import { runtimeSyncMessage, useAccountRuntimeSync } from '@/features/account/useAccountRuntimeSync'
import { AlgorithmWorkbench } from '@/features/account/AlgorithmWorkbench'
import { algorithmScheduleDraft, algorithmScheduleValue, algorithmScheduleErrors } from '@/features/account/algorithmSchedule'
import {
  EditError,
  EditLoading,
  EditSaveBar,
  Toggle,
} from '@/features/account/editUi'
import type { Account } from '@/types/api'

function norm(v: unknown): string {
  return JSON.stringify(v ?? null)
}

function algoParamError(algo: AlgorithmRef | null): string | null {
  if (!algo) return null
  return validateAlgorithmRef(algo)
}

export function AccountEditAlgorithmPage() {
  const { id } = useParams()
  return <AccountAlgorithmForm key={id} accountId={Number(id)} />
}

function AccountAlgorithmForm({ accountId }: { accountId: number }) {
  const accountPreview = readAlgorithmAccountPreview(accountId)
  const runtime = useAccountRuntimeSync(accountId)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const toast = useToastStore((s) => s.toast)
  const accounts = useDomainStore((s) => s.accounts)
  const refreshAccounts = useDomainStore((s) => s.refreshAccounts)
  const account = usePolling(useCallback((s: AbortSignal) => getAccount(accountId, s), [accountId]), {
    queryKey: `account:${accountId}`,
    intervalMs: 0,
  })
  const [savedAccount, setSavedAccount] = useState<Account | null>(null)
  const acc = savedAccount ?? account.data ?? accountPreview
  const cachedAccount = accounts?.find((item) => item.account_id === accountId) ?? null
  const descriptor = useChannelDescriptor(acc?.trade_channel)

  // Hero 配置带算法名 ↔ 对应选择器选中名称；首帧靠详情预览保留落点。
  const tSelf = useViewTransitionState(`/accounts/${accountId}/edit/algorithm`)
  const tDetail = useViewTransitionState(`/accounts/${accountId}`)
  useEffect(() => {
    if (acc && descriptor) {
      writeAccountConfigSummary(accountId, acc, {
        showShortLeverage: descriptor.ui.show_short_leverage,
      })
    }
  }, [acc, descriptor, accountId])
  const title = (
    <div className="flex flex-wrap items-center gap-3">
      <AccountPageTitle
        accountId={accountId}
        page="执行算法"
        name={acc?.name ?? cachedAccount?.name}
        channel={acc?.trade_channel ?? cachedAccount?.trade_channel}
        market={acc?.market ?? cachedAccount?.market}
      />
    </div>
  )

  const [ready, setReady] = useState(() => algorithmRefOf(accountPreview?.algorithm) !== null)
  const [trade, setTrade] = useState<AlgorithmRef | null>(() => algorithmRefOf(accountPreview?.algorithm))
  const [empty, setEmpty] = useState<AlgorithmRef | null>(() => algorithmRefOf(accountPreview?.empty_positions_algorithm))
  const [schedule, setSchedule] = useState(() => algorithmScheduleDraft(accountPreview?.algorithm_schedule ?? []))
  const [closePolicy, setClosePolicy] = useState(accountPreview?.futures_close_policy ?? defaultClosePolicy())
  const [replayRecording, setReplayRecording] = useState(accountPreview?.replay_recording_enabled ?? false)
  const [saving, setSaving] = useState(false)
  const [editorErrors, setEditorErrors] = useState<Record<string, string | null>>({})
  const setEditorError = useCallback((key: string, error: string | null) => {
    setEditorErrors(previous => previous[key] === error ? previous : { ...previous, [key]: error })
  }, [])
  const [saveError, setSaveError] = useState<Error | null>(null)

  /** 草稿重置为服务端当前算法引用；数据未就绪时返回 false（供首帧初始化门控）。 */
  const resetAlgorithms = useCallback(() => {
    if (!acc || !descriptor) return false
    setTrade(structuredClone(algorithmRefOf(acc.algorithm) ?? descriptor.defaults.trade_algorithm))
    setEmpty(structuredClone(algorithmRefOf(acc.empty_positions_algorithm)))
    setEditorErrors({})
    setSchedule(previous => algorithmScheduleDraft(acc.algorithm_schedule ?? [], previous))
    setClosePolicy(acc.futures_close_policy ?? defaultClosePolicy())
    setReplayRecording(acc.replay_recording_enabled ?? false)
    setSaveError(null)
    return true
  }, [acc, descriptor])

  useEffect(() => {
    if (!ready && resetAlgorithms()) setReady(true)
  }, [ready, resetAlgorithms])

  if (account.error && !acc)
    return <EditError error={account.error} onRetry={account.refresh} />

  const isReady = !(!ready || !acc || !trade || !descriptor)
  const tradeSame = trade == null || describeAlgorithmRef(trade) === describeAlgorithmRef(algorithmRefOf(acc?.algorithm))
  const emptySame = !isReady || describeAlgorithmRef(empty) === describeAlgorithmRef(algorithmRefOf(acc?.empty_positions_algorithm))
  const activeSlot = algorithmTransitionSlot(accountId)
  const tradeNameVt = tSelf && tDetail && activeSlot === 'trade' && tradeSame && schedule.length === 0
    ? accountConfigVtName(accountId, 'algorithm') : undefined
  const emptyNameVt = tSelf && tDetail && activeSlot === 'empty' && emptySame
    ? accountConfigVtName(accountId, 'empty') : undefined

  if (!isReady)
    return (
      <section className="px-4 pt-4 md:px-5">
        {title}
        <EditLoading bare />
      </section>
    )

  const origTrade = algorithmRefOf(acc.algorithm) ?? descriptor.defaults.trade_algorithm
  const origEmpty = algorithmRefOf(acc.empty_positions_algorithm)

  const tradeChanged = norm(trade) !== norm(origTrade)
  const emptyChanged = norm(empty) !== norm(origEmpty)
  const scheduleValue = algorithmScheduleValue(schedule)
  const scheduleChanged = norm(scheduleValue) !== norm([...(acc.algorithm_schedule ?? [])].sort((a, b) => a.start.localeCompare(b.start)))
  const closeChanged = norm(closePolicy) !== norm(acc.futures_close_policy ?? defaultClosePolicy())
  const recordingPatch = replayRecordingPatch(replayRecording, acc.replay_recording_enabled)
  const recordingChanged = recordingPatch.replay_recording_enabled !== undefined
  const dirty = tradeChanged || emptyChanged || scheduleChanged || closeChanged || recordingChanged

  const err = ['trade', 'empty', ...schedule.map(rule => rule.key)].map(key => editorErrors[key]).find(Boolean)
    ?? algorithmScheduleErrors(schedule).find(Boolean) ?? algoParamError(trade) ?? algoParamError(empty)
  const changes: string[] = []
  if (recordingChanged) changes.push(`执行回放录制：${replayRecording ? '开启' : '关闭'}`)
  if (closeChanged) changes.push('平仓规则已改')
  if (tradeChanged) changes.push('主交易算法已改')
  if (emptyChanged) changes.push(empty ? '清仓算法已改' : '清仓算法已清除')
  if (scheduleChanged) changes.push('交易时段已改')
  if (err && changes.length === 0) changes.push('参数编辑尚未完成')

  const save = async () => {
    if (err) return toast(err)
    if (!dirty) return toast('没有改动')
    const patch: Partial<Account> = { ...recordingPatch }
    if (tradeChanged) patch.algorithm = trade as unknown as Record<string, unknown>
    if (scheduleChanged) patch.algorithm_schedule = scheduleValue
    if (emptyChanged) {
      patch.empty_positions_algorithm = empty
        ? (empty as unknown as Record<string, unknown>)
        : null
    }
    if (closeChanged) patch.futures_close_policy = closePolicy
    if (saving) return
    runtime.invalidate()
    setSaving(true)
    setSaveError(null)
    try {
      const updated = await updateAccount(accountId, patch)
      if (!alive.current) return
      setSavedAccount(updated)
      writeAlgorithmAccountPreview(accountId, updated)
      setTrade(algorithmRefOf(updated.algorithm) ?? descriptor.defaults.trade_algorithm)
      setEmpty(algorithmRefOf(updated.empty_positions_algorithm))
      setSchedule(previous => algorithmScheduleDraft(updated.algorithm_schedule ?? [], previous))
      setEditorErrors({})
      setClosePolicy(updated.futures_close_policy ?? defaultClosePolicy())
      setReplayRecording(updated.replay_recording_enabled ?? false)
      runtime.acceptSaved(updated.runtime_sync)
      // 保存响应直接写摘要缓存：返回详情时 hero「算法」值首帧即新值，FLIP 落地同文。
      writeAccountConfigSummary(accountId, updated, {
        showShortLeverage: descriptor.ui.show_short_leverage,
      })
      toast(tradeChanged || emptyChanged || closeChanged || scheduleChanged
        ? `已保存，后续调仓生效。${shortErrorReason(runtimeSyncMessage(updated.runtime_sync))}`
        : '录制设置已保存，实际状态请看录制开关旁的提示。')
      void refreshAccounts()
    } catch (e) {
      if (alive.current) {
        setSaveError(e instanceof Error ? e : new Error(String(e)))
        void runtime.refresh()
      }
    } finally {
      if (alive.current) setSaving(false)
    }
  }

  return (
    <section aria-label="执行算法设置" className="grid h-full min-h-0 min-w-0 grid-rows-[auto_minmax(0,1fr)_auto] gap-y-2 px-4 pt-4 md:px-5">
      <header>
        {title}
        <RuntimeSyncStatus runtime={runtime} disabled={saving} accountId={accountId} />
      </header>

      <fieldset disabled={saving} className="min-h-0 min-w-0 overflow-hidden" inert={saving}>
        <AlgorithmWorkbench
          channel={acc.trade_channel} trade={trade} empty={empty} schedule={schedule}
          initialSlot={activeSlot} tradeNameVt={tradeNameVt} emptyNameVt={emptyNameVt}
          onTradeChange={value => { setSaveError(null); setTrade(value) }}
          onEmptyChange={value => { setSaveError(null); setEmpty(value) }}
          onScheduleChange={value => { setSaveError(null); setSchedule(value) }}
          errors={editorErrors} onValidationError={setEditorError}
          footer={<div className="space-y-2 pb-1">
            {descriptor.futures_close && <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 border-t border-line pt-2">
              <span className="shrink-0 text-xs text-ink-3">平仓规则</span>
              <FuturesClosePolicyEditor value={closePolicy} capability={descriptor.futures_close}
                onChange={value => { setSaveError(null); setClosePolicy(value) }} />
            </div>}
            <div className="border-t border-line pt-2" role="group" aria-label="执行回放录制">
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 flex-wrap items-center gap-1 text-sm">
                  <span>回放录制</span>
                  <Tooltip content="记录行情、委托和成交，方便之后回看执行过程。保存后开始记录，之前的数据不会补录。">
                    <button type="button" aria-label="执行回放录制说明" className="inline-flex size-6 items-center justify-center rounded-chip text-ink-3 hover:bg-fill focus-visible:outline-2 focus-visible:outline-accent">
                      <Info size={14} aria-hidden />
                    </button>
                  </Tooltip>
                  {recordingChanged && <span className="text-xs text-ink-3">待保存</span>}
                  <ReplayRecordingStatus accountId={accountId} savedEnabled={acc.replay_recording_enabled ?? false} />
                </div>
                <Toggle on={replayRecording} onClick={() => { setSaveError(null); setReplayRecording(!replayRecording) }} ariaLabel="记录执行回放" />
              </div>
            </div>
          </div>}
        />
      </fieldset>

      <EditSaveBar layout="inline" changes={changes} blocked={Boolean(err)}
        onCancel={() => { if (!saving) resetAlgorithms() }} onSave={() => void save()}
        saving={saving} error={saveError} />
    </section>
  )
}
