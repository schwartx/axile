/** 组合相关接口。 */
import { apiGet, apiSend } from '@/lib/api/client'
import type {
  Message,
  Portfolio,
  PortfolioList,
  TargetWeightSnapshot,
  ValidateCustomCalcResult,
} from '@/types/api'

/** 组合列表（不含策略）。 */
export function getPortfolios(signal?: AbortSignal): Promise<PortfolioList> {
  return apiGet<PortfolioList>('/portfolio/', signal)
}

/** 组合详情（含当前策略与绑定账户）。 */
export function getPortfolio(id: number, signal?: AbortSignal): Promise<Portfolio> {
  return apiGet<Portfolio>(`/portfolio/${id}`, signal)
}

/** 只读组合最近一次成功计算的原始目标快照。 */
export function getPortfolioTargetSnapshot(id: number, signal?: AbortSignal): Promise<TargetWeightSnapshot> {
  return apiGet<TargetWeightSnapshot>(`/portfolio/${id}/target_snapshot`, signal)
}

/** 主动执行组合函数并保存原始目标快照。 */
export function refreshPortfolioTargetSnapshot(id: number): Promise<TargetWeightSnapshot> {
  return apiSend<TargetWeightSnapshot>('POST', `/portfolio/${id}/target_snapshot/refresh`)
}

/**
 * 校验一段自定义组合脚本（不落库、不自动调仓；脚本主动调用 executor 除外）。
 *
 * 执行用户脚本里的 `calculate_portfolio(context)` 一次并返回目标权重或错误信息。
 * 不传 `account_id` 走样例上下文（Dry-run）；传则借该账户构造真实上下文执行。
 * 脚本错误以 `{ valid: false }` 结构化返回（HTTP 200）；仅账户不存在等调用错误会抛 `ApiError`。
 */
export function validateCustomCalc(body: {
  custom_calc_py_code: string
  account_id?: number | null
}): Promise<ValidateCustomCalcResult> {
  return apiSend<ValidateCustomCalcResult>('POST', '/portfolio/validate_custom_calc', body)
}

/** 创建组合。 */
export function createPortfolio(body: {
  name: string
  market: string
  description?: string | null
  custom_calc_py_code: string
  status?: string | null
  tag?: string | null
}): Promise<Portfolio> {
  // 后端 PortfolioCreate 要求这些字段「存在」（即便为 null），补齐默认值。
  return apiSend<Portfolio>('POST', '/portfolio/', {
    description: null,
    status: null,
    tag: null,
    ...body,
  })
}

/** 更新组合（局部）。 */
export function updatePortfolio(
  id: number,
  patch: Partial<{
    name: string
    market: string
    description: string | null
    custom_calc_py_code: string
    status: string | null
    tag: string | null
  }>,
): Promise<Portfolio> {
  return apiSend<Portfolio>('PATCH', `/portfolio/${id}`, patch)
}

/** 删除组合。 */
export function deletePortfolio(id: number): Promise<Message> {
  return apiSend<Message>('DELETE', `/portfolio/${id}`)
}

/** 试跑当前草稿，通过后同时保存代码与原始目标快照。 */
export function runAndSavePortfolio(id: number, body: {
  name: string
  custom_calc_py_code: string
  account_id: number | null
}): Promise<ValidateCustomCalcResult> {
  return apiSend<ValidateCustomCalcResult>('POST', `/portfolio/${id}/run_and_save`, body)
}
