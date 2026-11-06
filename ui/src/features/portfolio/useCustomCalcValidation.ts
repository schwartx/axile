import { useCallback, useState } from 'react'
import { usePythonRun } from '@/components/ui/pythonWorkbenchState'
import type { PythonValidationState } from '@/components/ui/PythonFunctionEditor'
import { validateCustomCalc } from '@/lib/api/portfolios'
import { useChannelCatalogStore } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import { useAccountConnection } from '@/stores/liveConnection'
import { useConnectionOperation } from '@/stores/connectionOperations'
import type { AccountDashboardItem, TrialLeverageOverrides, ValidateCustomCalcResult } from '@/types/api'

export interface CustomCalcValidation {
  validating: boolean
  /** 原始校验结果（含 target 权重），供结果区渲染。 */
  result: ValidateCustomCalcResult | null
  /** 编辑器直接消费的状态视图（错误行定位 / traceback）。 */
  editorResult: PythonValidationState | null
  /** 代码在最后一次试跑后又改过：结果保留展示但整体降级，不冒充新结论。 */
  stale: boolean
  accountId: number | null
  setAccountId: (id: number | null) => void
  accounts: AccountDashboardItem[]
  blockedReason: string | null
  leverageEditing: boolean
  setLeverageDraft: (accountId: number, overrides: TrialLeverageOverrides | null, invalid: boolean) => void
  canRun: boolean
  run: (execute?: (code: string) => Promise<ValidateCustomCalcResult>) => Promise<ValidateCustomCalcResult | null>
}

/**
 * 「试跑目标计算函数」状态机：代码 + 试跑上下文 → 校验结果。
 * 改代码不清空旧结果，仅降级 stale（清空收放会让打字中的代码跳）。
 * 工作台编辑页与初始化向导（console 布局）共用这一份逻辑。
 */
export function useCustomCalcValidation(code: string, market: string | null | undefined): CustomCalcValidation {
  const [accountId, setAccountId] = useState<number | null>(null)
  const [leverageDrafts, setLeverageDrafts] = useState<Record<number, { overrides: TrialLeverageOverrides | null; invalid: boolean }>>({})
  const setLeverageDraft = useCallback((id: number, overrides: TrialLeverageOverrides | null, invalid: boolean) => {
    setLeverageDrafts((previous) => ({ ...previous, [id]: { overrides, invalid } }))
  }, [])
  const accounts = useDomainStore((state) => state.accounts) ?? []
  const channels = useChannelCatalogStore((state) => state.channels)
  const marketId = (value: string) => channels?.find(
    (channel) => channel.market === value || channel.portfolio.market_label === value,
  )?.market ?? value
  const matchingAccounts = accounts.filter((account) => market != null && marketId(account.market) === marketId(market))
  const contextAccountId = matchingAccounts.some((account) => account.account_id === accountId) ? accountId : null
  const selected = matchingAccounts.find((account) => account.account_id === contextAccountId)
  const channel = selected ? channels?.find((item) => item.channel === selected.trade_channel) : undefined
  const leverageDraft = leverageDrafts[contextAccountId ?? -1]
  const overrides = leverageDraft?.overrides ?? null
  const connection = useAccountConnection(contextAccountId ?? -1)
  const operation = useConnectionOperation(contextAccountId ?? -1)
  const blockedReason = contextAccountId === null ? null
    : leverageDraft?.invalid ? '杠杆参数有误，请先修正'
      : operation.pending !== null || connection.status === 'connecting' || connection.status === 'recovering' ? '账户连接操作中，请稍候'
        : connection.status === 'ready' ? null
          : connection.status === 'unknown' ? '正在确认账户连接状态'
            : '请先连接账户，确认就绪后再试跑'
  const trial = usePythonRun<ValidateCustomCalcResult>({
    code, contextKey: JSON.stringify([contextAccountId,
      overrides?.long_leverage ?? selected?.long_leverage ?? channel?.defaults.long_leverage,
      overrides?.short_leverage ?? selected?.short_leverage ?? channel?.defaults.short_leverage]),
    enabled: blockedReason === null,
    execute: (draft) => validateCustomCalc({ custom_calc_py_code: draft, account_id: contextAccountId, leverage_overrides: overrides }),
    failed: (error) => {
      const message = error instanceof Error ? error.message : String(error)
      return { valid: false, target: null, error: message, traceback: null, error_line: null,
        error_offset: null, error_type: null, error_message: message }
    },
    toEditorResult: (result) => ({ valid: result.valid, errorLine: result.error_line,
      errorType: result.error_type, errorMessage: result.error_message, traceback: result.traceback }),
  })
  const { running: validating, result, editorResult, stale, run } = trial

  return {
    validating,
    result,
    editorResult,
    stale,
    accountId: contextAccountId,
    setAccountId,
    accounts: matchingAccounts,
    blockedReason,
    leverageEditing: !!(overrides || leverageDraft?.invalid),
    setLeverageDraft,
    canRun: trial.canRun,
    run,
  }
}
