import { shortErrorReason } from '@/lib/errorInfo'
/**
 * 账户执行算法子页 /accounts/:id/edit/algorithm。
 *
 * 主交易 + 清仓算法完整编辑器；保存只 PATCH 算法相关字段，保存与取消都不离开本页。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams, useViewTransitionState } from 'react-router'
import { getAccount, updateAccount } from '@/lib/api/accounts'
import { usePolling } from '@/lib/hooks/usePolling'
import { useDomainStore } from '@/stores/domain'
import { useToastStore } from '@/stores/ui'
import { AccountPageTitle } from '@/features/account/pageHead'
import { useChannelDescriptor } from '@/stores/channels'
import {
  accountConfigVtName,
  readAccountConfigSummary,
  writeAccountConfigSummary,
} from '@/features/account/configSummary'
import { algorithmTransitionSlot, readAlgorithmAccountPreview, writeAlgorithmAccountPreview } from '@/features/account/algorithmNameTransition'
import {
  algorithmRefOf,
  describeAlgorithmRef,
  validateAlgorithmRef,
  type AlgorithmRef,
} from '@/features/setup/algorithms'
import { RuntimeSyncStatus } from '@/features/account/RuntimeSyncStatus'
import { runtimeSyncMessage, useAccountRuntimeSync } from '@/features/account/useAccountRuntimeSync'
import { AlgorithmEditor } from '@/features/setup/AlgorithmEditor'
import {
  EditError,
  EditLoading,
  EditSaveBar,
  EditSynopsis,
  Row,
  Section,
} from '@/features/account/editUi'
import type { Account, AlgorithmSlot } from '@/types/api'

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
  const cachedConfig = acc ? null : readAccountConfigSummary(accountId)
  useEffect(() => {
    if (acc && descriptor) {
      writeAccountConfigSummary(accountId, acc, {
        showShortLeverage: descriptor.ui.show_short_leverage,
      })
    }
  }, [acc, descriptor, accountId])
  const title = (
    <div className="flex flex-wrap items-baseline gap-3">
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
  const [saving, setSaving] = useState(false)
  const [tradeError, setTradeError] = useState<string | null>(null)
  const [emptyError, setEmptyError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<Error | null>(null)

  /** 草稿重置为服务端当前算法引用；数据未就绪时返回 false（供首帧初始化门控）。 */
  const resetAlgorithms = useCallback(() => {
    if (!acc || !descriptor) return false
    setTrade(algorithmRefOf(acc.algorithm) ?? descriptor.defaults.trade_algorithm)
    setEmpty(algorithmRefOf(acc.empty_positions_algorithm))
    setSaveError(null)
    return true
  }, [acc, descriptor])

  useEffect(() => {
    if (!ready && resetAlgorithms()) setReady(true)
  }, [ready, resetAlgorithms])

  if (account.error && !acc)
    return <EditError error={account.error} onRetry={account.refresh} />

  const isReady = !(!ready || !acc || !trade || !descriptor)
  // 「当前配置」摘要沿用 草稿 → 真源 → 缓存 的兜底链，加载期间保持内容稳定。
  // 具名捕获现在落在选择器的算法名称；详情预览使选择器首帧就能渲染。
  const savedTradeSum = acc ? describeAlgorithmRef(algorithmRefOf(acc.algorithm)) : null
  const tradeSynopsis = trade
    ? describeAlgorithmRef(trade)
    : (savedTradeSum ?? cachedConfig?.algorithm ?? null)
  const savedEmptySum = acc ? describeAlgorithmRef(algorithmRefOf(acc.empty_positions_algorithm)) : null
  // 清仓草稿可合法为 null（未设置）；未就绪时走真源 / 缓存，保证摘要节点常挂、FLIP 有落点。
  const emptySynopsis = isReady
    ? describeAlgorithmRef(empty)
    : (savedEmptySum ?? cachedConfig?.emptyAlgorithm ?? null)
  // 草稿与持久配置同文时才让所点击的算法名共享；避免未保存选择飞向旧名称。
  const tradeSame = trade == null || tradeSynopsis === savedTradeSum
  const emptySame = !isReady || describeAlgorithmRef(empty) === savedEmptySum
  const activeSlot = algorithmTransitionSlot(accountId)
  const tradeNameVt = tSelf && tDetail && activeSlot === 'trade' && tradeSame
    ? accountConfigVtName(accountId, 'algorithm') : undefined
  const emptyNameVt = tSelf && tDetail && activeSlot === 'empty' && emptySame
    ? accountConfigVtName(accountId, 'empty') : undefined

  /** 标题 + 当前配置摘要：加载与就绪两态共用（同位同节点）；下单 / 清仓都常挂。 */
  const synopsis = tradeSynopsis != null && (
    <EditSynopsis note={isReady ? '保存后用于后续执行，已在途任务不变。' : undefined}>
      <div className="grid grid-cols-[3rem_minmax(0,1fr)] gap-x-3 gap-y-0.5">
        <span className="font-normal text-ink-3">下单</span>
        <span className="inline-block">
          {tradeSynopsis}
        </span>
        <span className="font-normal text-ink-3">清仓</span>
        <span className="inline-block">
          {emptySynopsis ?? '—'}
        </span>
      </div>
    </EditSynopsis>
  )

  if (!isReady)
    return (
      <section className="pb-24">
        {title}
        {synopsis}
        <EditLoading bare />
      </section>
    )

  const origTrade = algorithmRefOf(acc.algorithm) ?? descriptor.defaults.trade_algorithm
  const origEmpty = algorithmRefOf(acc.empty_positions_algorithm)

  const tradeChanged = norm(trade) !== norm(origTrade)
  const emptyChanged = norm(empty) !== norm(origEmpty)
  const dirty = tradeChanged || emptyChanged

  const err = tradeError ?? emptyError ?? algoParamError(trade) ?? algoParamError(empty)
  const changes: string[] = []
  if (tradeChanged) changes.push('主交易算法已改')
  if (emptyChanged) changes.push(empty ? '清仓算法已改' : '清仓算法已清除')

  const save = async () => {
    if (err) return toast(`算法参数非法：${err}`)
    if (!dirty) return toast('没有改动')
    const patch: Partial<Account> = {}
    if (tradeChanged) patch.algorithm = trade as unknown as Record<string, unknown>
    if (emptyChanged) {
      patch.empty_positions_algorithm = empty
        ? (empty as unknown as Record<string, unknown>)
        : null
    }
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
      runtime.acceptSaved(updated.runtime_sync)
      // 保存响应直接写摘要缓存：返回详情时 hero「算法」值首帧即新值，FLIP 落地同文。
      writeAccountConfigSummary(accountId, updated, {
        showShortLeverage: descriptor.ui.show_short_leverage,
      })
      toast(shortErrorReason(runtimeSyncMessage(updated.runtime_sync)))
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

  const slotRow = (
    slot: AlgorithmSlot,
    label: string,
    value: AlgorithmRef | null,
    onChange: (v: AlgorithmRef | null) => void,
  ) => (
    <Row label={label} top span>
      <AlgorithmEditor
        nameVtName={slot === 'trade' ? tradeNameVt : emptyNameVt}
        onValidationError={slot === 'trade' ? setTradeError : setEmptyError}
        slot={slot}
        channel={acc.trade_channel}
        value={slot === 'trade' ? (value ?? descriptor.defaults.trade_algorithm) : value}
        allowClear={slot === 'empty'}
        onChange={onChange}
      />
    </Row>
  )

  return (
    <section>
      {title}
      {synopsis}

      <RuntimeSyncStatus runtime={runtime} disabled={saving} accountId={accountId} />

      <fieldset disabled={saving} className="min-w-0" inert={saving}>
        <Section label="主交易算法">
          <p className="mb-3 text-sm text-ink-3">用于常规定时调仓。</p>
          {slotRow('trade', '算法', trade, (v) => { setSaveError(null); setTrade(v ?? descriptor.defaults.trade_algorithm) })}
        </Section>
        <Section label="清仓算法">
          <p className="mb-3 text-sm text-ink-3">用于清仓执行。</p>
          {slotRow('empty', '算法', empty, (value) => { setSaveError(null); setEmpty(value) })}
        </Section>

      </fieldset>

      <EditSaveBar
        changes={changes}
        blocked={Boolean(err)}
        onCancel={() => { if (!saving) resetAlgorithms() }}
        onSave={() => void save()}
        saving={saving}
        error={saveError}
      />
    </section>
  )
}
