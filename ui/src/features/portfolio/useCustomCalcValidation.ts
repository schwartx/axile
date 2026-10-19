import { useState } from 'react'
import { usePythonRun } from '@/components/ui/pythonWorkbenchState'
import type { PythonValidationState } from '@/components/ui/PythonFunctionEditor'
import type { SelectOption } from '@/components/ui/Select'
import { channelLabel } from '@/features/dashboard/display'
import { validateCustomCalc } from '@/lib/api/portfolios'
import { useDomainStore } from '@/stores/domain'
import type { ValidateCustomCalcResult } from '@/types/api'

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
  contextOptions: SelectOption<number | null>[]
  canRun: boolean
  run: (execute?: (code: string) => Promise<ValidateCustomCalcResult>) => Promise<ValidateCustomCalcResult | null>
}

/**
 * 「试跑目标计算函数」状态机：代码 + 试跑上下文 → 校验结果。
 * 改代码不清空旧结果，仅降级 stale（清空收放会让打字中的代码跳）。
 * 工作台编辑页与初始化向导（console 布局）共用这一份逻辑。
 */
export function useCustomCalcValidation(code: string): CustomCalcValidation {
  const [accountId, setAccountId] = useState<number | null>(null)
  const accounts = useDomainStore((state) => state.accounts) ?? []
  const trial = usePythonRun<ValidateCustomCalcResult>({
    code, contextKey: String(accountId),
    execute: (draft) => validateCustomCalc({ custom_calc_py_code: draft, account_id: accountId }),
    failed: (error) => {
      const message = error instanceof Error ? error.message : String(error)
      return { valid: false, target: null, error: message, traceback: null, error_line: null,
        error_offset: null, error_type: null, error_message: message }
    },
    toEditorResult: (result) => ({ valid: result.valid, errorLine: result.error_line,
      errorType: result.error_type, errorMessage: result.error_message, traceback: result.traceback }),
  })
  const { running: validating, result, editorResult, stale, run } = trial

  // 选项自带说明：样例 = 安全沙箱（假数据、不连渠道）；账户 = 真实数据 + 真实副作用风险。
  // 控件只说「样例上下文」会把这两级语义全吞掉。
  const contextOptions: SelectOption<number | null>[] = [
    { value: null, label: '样例数据', description: '固定样例资产，价格恒为 100，不连接真实渠道' },
    ...accounts.map((account) => ({
      value: account.account_id,
      label: account.name,
      description: '真实持仓与行情 · 主动交易会产生真实委托',
      hint: channelLabel(account.trade_channel, account.market),
    })),
  ]

  return {
    validating,
    result,
    editorResult,
    stale,
    accountId,
    setAccountId,
    contextOptions,
    canRun: trial.canRun,
    run,
  }
}
