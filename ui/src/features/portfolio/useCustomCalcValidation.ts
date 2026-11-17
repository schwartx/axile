import { useCallback, useState } from 'react'
import { usePythonRun } from '@/components/ui/pythonWorkbenchState'
import type { PythonValidationState } from '@/components/ui/PythonFunctionEditor'
import { validateCustomCalc } from '@/lib/api/portfolios'
import { useChannelCatalogStore } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'
import { useSimulationAccounts } from '@/stores/simulationAccounts'
import { useAccountConnection } from '@/stores/liveConnection'
import { useConnectionOperation } from '@/stores/connectionOperations'
import type { AccountDashboardItem, ChannelCapability, SimulationAccount, TrialLeverageOverrides, ValidateCustomCalcResult } from '@/types/api'

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
  simulationChannels: ChannelCapability[]
  simulationChannel: ChannelCapability | undefined
  simulationAccount: SimulationAccount | null
  setSimulationChannel: (channel: string) => void
  blockedReason: string | null
  leverageEditing: boolean
  setLeverageDraft: (accountId: number | null, overrides: TrialLeverageOverrides | null, invalid: boolean) => void
  canRun: boolean
  run: (execute?: (code: string) => Promise<ValidateCustomCalcResult>) => Promise<ValidateCustomCalcResult | null>
}

/**
 * 「试跑目标计算函数」状态机：代码 + 试跑上下文 → 校验结果。
 * 改代码不清空旧结果，仅降级 stale（清空收放会让打字中的代码跳）。
 * 工作台编辑页与初始化向导（console 布局）共用这一份逻辑。
 */
export function useCustomCalcValidation(code: string, market: string | null | undefined, portfolioId?: number): CustomCalcValidation {
  const [selection, setSelection] = useState<{ portfolioId: number | undefined; accountId: number | null; simulationChannel?: string } | null>(null)
  const [leverageDrafts, setLeverageDrafts] = useState<Record<string, { overrides: TrialLeverageOverrides | null; invalid: boolean }>>({})
  const setLeverageDraft = useCallback((id: number | null, overrides: TrialLeverageOverrides | null, invalid: boolean) => {
    setLeverageDrafts((previous) => ({ ...previous, [id ?? 'sample']: { overrides, invalid } }))
  }, [])
  const accounts = useDomainStore((state) => state.accounts) ?? []
  const channels = useChannelCatalogStore((state) => state.channels)
  const marketId = (value: string) => channels?.find(
    (channel) => channel.market === value || channel.portfolio.market_label === value,
  )?.market ?? value
  const matchingAccounts = accounts.filter((account) => market != null && marketId(account.market) === marketId(market))
  const accountId = selection && selection.portfolioId === portfolioId ? selection.accountId : null
  const contextAccountId = matchingAccounts.some((account) => account.account_id === accountId) ? accountId : null
  const simulationChannels = (channels ?? []).filter((channel) => channel.simulation_account
    && market != null && marketId(channel.market) === marketId(market))
  const simulationChannel = simulationChannels.find((channel) => selection?.portfolioId === portfolioId
    && channel.channel === selection?.simulationChannel) ?? simulationChannels[0]
  const savedSimulations = useSimulationAccounts((state) => state.accounts)
  const simulationAccount = simulationChannel
    ? savedSimulations[simulationChannel.channel] ?? simulationChannel.simulation_account ?? null : null
  const selected = matchingAccounts.find((account) => account.account_id === contextAccountId)
  const channel = selected ? channels?.find((item) => item.channel === selected.trade_channel) : undefined
  const leverageDraft = leverageDrafts[contextAccountId ?? 'sample']
  const overrides = leverageDraft?.overrides ?? null
  const connection = useAccountConnection(contextAccountId ?? -1)
  const operation = useConnectionOperation(contextAccountId ?? -1)
  const blockedReason = leverageDraft?.invalid ? '杠杆参数有误，请先修正'
    : contextAccountId === null ? simulationAccount ? null : '正在加载渠道模拟账户，请稍候'
      : operation.pending !== null || connection.status === 'connecting' || connection.status === 'recovering' ? '账户连接操作中，请稍候'
        : connection.status === 'ready' ? null
          : connection.status === 'unknown' ? '正在确认账户连接状态'
            : '请先连接账户，确认就绪后再试跑'
  const trial = usePythonRun<ValidateCustomCalcResult>({
    code, contextKey: JSON.stringify(contextAccountId === null ? [simulationChannel?.channel, simulationAccount] : [contextAccountId,
      overrides?.long_leverage ?? selected?.long_leverage ?? channel?.defaults.long_leverage ?? 1,
      overrides?.short_leverage ?? selected?.short_leverage ?? channel?.defaults.short_leverage ?? 1]),
    enabled: blockedReason === null,
    execute: (draft) => validateCustomCalc(contextAccountId === null
      ? { custom_calc_py_code: draft, account_id: null, simulation_channel: simulationChannel?.channel, simulation_account: simulationAccount }
      : { custom_calc_py_code: draft, account_id: contextAccountId, leverage_overrides: overrides }),
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
    setAccountId: (accountId) => setSelection({ portfolioId, accountId }),
    accounts: matchingAccounts,
    simulationChannels,
    simulationChannel,
    simulationAccount,
    setSimulationChannel: (simulationChannel) => setSelection({ portfolioId, accountId: null, simulationChannel }),
    blockedReason,
    leverageEditing: !!(overrides || leverageDraft?.invalid),
    setLeverageDraft,
    canRun: trial.canRun,
    run,
  }
}
