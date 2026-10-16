/** 算法引用校验与服务端展示名；不在前端维护算法说明或交易意图预设。 */
import type { AlgorithmInfo, AlgorithmRef } from '@/types/api'
import { validateAlgorithmSchemaParams } from '@/features/setup/algorithmSchema'

export type { AlgorithmRef } from '@/types/api'

export const SINGLE_MAKER_DEFAULT_PARAMS: Readonly<Record<string, unknown>> = {
  price_strategy: 'ACTIVE',
  max_wait_seconds: 60,
  chase_enabled: false,
  chase_ticks: 1,
  max_chase_count: 5,
  chase_interval: 5,
  on_missing_book: 'skip',
}

export const TARGET_POS_DEFAULT_PARAMS: Readonly<Record<string, unknown>> = {
  price_strategy: 'PASSIVE',
  close_order_type: 'LIMIT',
  open_order_type: 'LIMIT',
  max_wait_seconds: 60,
  chase_enabled: false,
  chase_ticks: 1,
  max_chase_count: 5,
  chase_interval: 5,
}

export const TWAP_DEFAULT_PARAMS: Readonly<Record<string, unknown>> = {
  max_wait_seconds: 60,
  total_duration: 300,
  slices: 10,
  price_strategy: 'ACTIVE',
}

export const POV_DEFAULT_PARAMS: Readonly<Record<string, unknown>> = {
  max_wait_seconds: 60,
  participation_rate: 0.1,
  interval_seconds: 5,
  max_duration: 600,
  price_strategy: 'ACTIVE',
  complete_on_timeout: true,
}

const DEFAULT_PARAMS_BY_METHOD: Readonly<Record<string, Readonly<Record<string, unknown>>>> = {
  'SINGLE-MAKER': SINGLE_MAKER_DEFAULT_PARAMS,
  TWAP: TWAP_DEFAULT_PARAMS,
  POV: POV_DEFAULT_PARAMS,
  'TARGET-POS-TASK': TARGET_POS_DEFAULT_PARAMS,
}

/**
 * 校验算法参数是否满足后端约束，镜像 `BaseAlgorithmParams` / `ChaseParamsMixin`。
 *
 * 校验存在且相关的枚举、时长与追单参数，其余键忽略。返回首个违规文案；全部合法
 * 返回 `null`。用于在提交前拦下非法 params，避免重蹈「参数被后端吞成 dict、执行
 * 到算法内部才炸」的覆辙。
 */
export function validateAlgorithmParams(params: Record<string, unknown>, method?: string): string | null {
  const num = (k: string): number | undefined => (typeof params[k] === 'number' ? (params[k] as number) : undefined)
  const has = (k: string): boolean => Object.prototype.hasOwnProperty.call(params, k)
  const effective = { ...(method ? runtimeAlgorithmDefaults[method] ?? DEFAULT_PARAMS_BY_METHOD[method] : undefined), ...params }
  const effectiveNum = (k: string): number | undefined =>
    typeof effective[k] === 'number' ? (effective[k] as number) : undefined

  if (has('price_strategy') && params.price_strategy !== 'PASSIVE' && params.price_strategy !== 'ACTIVE') {
    return `下单价格（price_strategy）必须是 PASSIVE 或 ACTIVE`
  }
  if (method === 'TARGET-POS-TASK') {
    for (const key of ['close_order_type', 'open_order_type'] as const) {
      if (has(key) && params[key] !== 'LIMIT' && params[key] !== 'MARKET') {
        return `${key === 'close_order_type' ? '平仓订单' : '开仓订单'}（${key}）必须是 LIMIT 或 MARKET`
      }
    }
  }
  if (has('on_missing_book') && !['skip', 'active', 'market'].includes(String(params.on_missing_book))) {
    return `盘口缺失策略（on_missing_book）必须是 skip、active 或 market`
  }
  if (has('chase_enabled') && typeof params.chase_enabled !== 'boolean') {
    return `追单开关（chase_enabled）必须是布尔值`
  }

  const wait = num('max_wait_seconds')
  if (has('max_wait_seconds') && wait === undefined) return `单次等待时间（max_wait_seconds）必须是数字`
  if (wait !== undefined && (!Number.isInteger(wait) || wait < 1 || wait > 3600)) {
    return `单次等待时间（max_wait_seconds）需为 1–3600 的整数，当前 ${wait}`
  }

  // 切片类算法的数值参数：镜像各自 params 模型与编辑器 NumRow 的边界，
  // 与服务端校验同口径，避免「超范围」标红却仍能保存。
  const totalDuration = num('total_duration')
  if (has('total_duration') && totalDuration === undefined) return `total_duration 必须是数字`
  if (totalDuration !== undefined && (!Number.isInteger(totalDuration) || totalDuration < 1 || totalDuration > 86400)) {
    return `total_duration 需为 1–86400 的整数，当前 ${totalDuration}`
  }
  const slices = num('slices')
  if (has('slices') && slices === undefined) return `slices 必须是数字`
  if (slices !== undefined && (!Number.isInteger(slices) || slices < 1 || slices > 1000)) {
    return `slices 需为 1–1000 的整数，当前 ${slices}`
  }
  const participation = num('participation_rate')
  if (has('participation_rate') && participation === undefined) return `participation_rate 必须是数字`
  if (participation !== undefined && (participation <= 0 || participation > 1)) {
    return `participation_rate 需在 (0, 1]，当前 ${participation}`
  }
  const intervalSeconds = num('interval_seconds')
  if (has('interval_seconds') && intervalSeconds === undefined) return `interval_seconds 必须是数字`
  if (intervalSeconds !== undefined && intervalSeconds < 0.1) {
    return `interval_seconds 需 ≥ 0.1s，当前 ${intervalSeconds}`
  }
  const maxDuration = num('max_duration')
  if (has('max_duration') && maxDuration === undefined) return `max_duration 必须是数字`
  if (maxDuration !== undefined && (!Number.isInteger(maxDuration) || maxDuration < 1 || maxDuration > 86400)) {
    return `max_duration 需为 1–86400 的整数，当前 ${maxDuration}`
  }
  if (has('complete_on_timeout') && typeof params.complete_on_timeout !== 'boolean') {
    return `complete_on_timeout 必须是布尔值`
  }
  if (method === 'TWAP') {
    const duration = effectiveNum('total_duration')
    const sliceCount = effectiveNum('slices')
    if (duration !== undefined && sliceCount !== undefined && duration / sliceCount < 0.1) {
      return `单片间隔（total_duration / slices）需 ≥ 0.1s，当前 ${(duration / sliceCount).toFixed(4)}s`
    }
  }
  if (method === 'POV') {
    const duration = effectiveNum('max_duration')
    const interval = effectiveNum('interval_seconds')
    if (duration !== undefined && interval !== undefined && duration < interval) {
      return `max_duration（${duration}s）不应小于 interval_seconds（${interval}s）`
    }
  }

  if (params.chase_enabled === true) {
    const ticks = num('chase_ticks')
    const count = num('max_chase_count')
    const interval = num('chase_interval')
    if (has('chase_ticks') && ticks === undefined) return `价格偏离（chase_ticks）必须是数字`
    if (has('max_chase_count') && count === undefined) return `最大追单次数（max_chase_count）必须是数字`
    if (has('chase_interval') && interval === undefined) return `追单间隔（chase_interval）必须是数字`
    if (ticks !== undefined && (!Number.isInteger(ticks) || ticks < 1 || ticks > 100)) {
      return `启用追单时价格偏离（chase_ticks）需为 1–100 的整数，当前 ${ticks}`
    }
    if (count !== undefined && (!Number.isInteger(count) || count < 1 || count > 50)) {
      return `启用追单时最大追单次数（max_chase_count）需为 1–50 的整数，当前 ${count}`
    }
    if (interval !== undefined && (!Number.isFinite(interval) || interval < 0.1 || interval > 300)) {
      return `启用追单时追单间隔（chase_interval）需在 0.1–300s，当前 ${interval}`
    }
    if (count !== undefined && interval !== undefined && count * interval > 600) {
      return `追单总时长（max_chase_count×chase_interval=${count * interval}s）不应超过 600s`
    }
    if (interval !== undefined && count !== undefined && interval < 1 && count > 10) {
      return `追单间隔过短（<1s）且次数过多（>10），可能触发限频`
    }
  }
  return null
}

/** 按算法类型校验完整算法引用，包括算法特有的多字段约束。 */
export function validateAlgorithmRef(ref: AlgorithmRef): string | null {
  return validateAlgorithmParams(ref.params, ref.method) ??
    validateAlgorithmSchemaParams(ref.params, runtimeAlgorithmSchemas[ref.method] ?? {})
}

let runtimeAlgorithmLabels: Record<string, string> = {}
let runtimeAlgorithmSchemas: Record<string, Record<string, unknown>> = {}
let runtimeAlgorithmDefaults: Record<string, Record<string, unknown>> = {}

/** 更新后端算法目录中的运行时展示名。 */
export function registerAlgorithmLabels(algorithms: AlgorithmInfo[]): void {
  runtimeAlgorithmDefaults = Object.fromEntries(algorithms.map((algorithm) => [algorithm.name, algorithm.default_params]))
  runtimeAlgorithmLabels = Object.fromEntries(algorithms.map((algorithm) => [algorithm.name, algorithm.label]))
  runtimeAlgorithmSchemas = Object.fromEntries(algorithms.map((algorithm) => [algorithm.name, algorithm.params_schema]))
}

/** 返回某算法的展示标签；未知算法回退为其 method 原名。 */
export function algoLabel(method: string): string {
  const runtimeLabel = runtimeAlgorithmLabels[method]
  return runtimeLabel || method
}

/** 从账户接口的未知 JSON 安全还原算法引用；结构不符时返回 `null`。 */
export function algorithmRefOf(raw: unknown): AlgorithmRef | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as { method?: unknown; params?: unknown }
  if (typeof value.method !== 'string') return null
  return { method: value.method, params: (value.params ?? {}) as Record<string, unknown> }
}

/** 摘要统一显示服务端算法名，不再反推已移除的意图卡片。 */
export function describeAlgorithmRef(ref: AlgorithmRef | null | undefined): string {
  return ref?.method ? algoLabel(ref.method) : '未设置'
}
