/**
 * 账户配置人话摘要（杠杆 / 品种控制 / 下单算法 / 清仓算法）与其共享元素 FLIP 协议。
 *
 * 杠杆 / 品种值 ↔ 编辑页「当前配置」摘要；算法名 ↔ 对应算法选择器中的名称。
 * 同一文字的两个落点挂同一个 ``viewTransitionName`` 做平移 + 微缩。
 *
 * 落点缺失即假连续，而 ``usePolling`` 是组件本地状态、编辑页首帧必冷——故本模块
 * 持一份模块级缓存（AlgorithmEditor 的 ``_algosCache`` 同款先例）：任何页面拿到账户
 * 真源（轮询到位或保存响应）即写入，任何页面首帧同步读出，保证 FLIP 两端始终有落点。
 */
import type { Account } from '@/types/api'
import { algorithmRefOf, describeAlgorithmRef } from '@/features/setup/algorithms'
import { algorithmScheduleSummary } from '@/features/account/algorithmSchedule'

/** 参与 FLIP 的配置项种类；与 hero 配置带四件套一一对应。 */
export type AccountConfigKind = 'leverage' | 'symbols' | 'algorithm' | 'empty'

/**
 * Hero 配置带值 ↔ 编辑分区页对应落点的共享元素名。
 *
 * 两侧都用 ``useViewTransitionState`` 对目标编辑路径做精确门控（该 hook 对过渡的
 * current/next 双向匹配），去程与返程同一份判定，挂名自然一致。
 */
export function accountConfigVtName(accountId: number, kind: AccountConfigKind): string {
  return `account-config-${kind}-${accountId}`
}

/** 杠杆值统一显示：整数不带小数点，否则一位小数；未设置为 —。 */
function fmtLev(v: number | null): string {
  return v == null ? '—' : `${Number.isInteger(v) ? v : v.toFixed(1)}×`
}

/** 杠杆摘要；渠道不暴露空头档时只报多头。 */
export function describeLeverage(
  longLeverage: number | null,
  shortLeverage: number | null,
  showShort: boolean,
): string {
  return showShort
    ? `多 ${fmtLev(longLeverage)} / 空 ${fmtLev(shortLeverage)}`
    : fmtLev(longLeverage)
}

/** 品种控制摘要：无控制为「未设限」，否则按类计数。 */
export function describeSymbolControl(
  forbidden: readonly string[] | null,
  risk: readonly string[] | null,
): string {
  const forbiddenCount = forbidden?.length ?? 0
  const riskCount = risk?.length ?? 0
  if (forbiddenCount + riskCount === 0) return '未设限'
  return [
    forbiddenCount > 0 ? `禁投 ${forbiddenCount}` : null,
    riskCount > 0 ? `风险 ${riskCount}` : null,
  ]
    .filter(Boolean)
    .join('、')
}

/** 四项配置摘要的一份快照。 */
export interface AccountConfigSummary {
  leverage: string
  symbols: string
  /** 主交易（下单）算法摘要。 */
  algorithm: string
  /** 清仓算法摘要；未设置为「未设置」。 */
  emptyAlgorithm: string
}

const cache = new Map<number, AccountConfigSummary>()

/** 首帧同步读；未写入过为 null（调用方退骨架）。 */
export function readAccountConfigSummary(accountId: number): AccountConfigSummary | null {
  return cache.get(accountId) ?? null
}

/**
 * 由账户真源计算并写入摘要缓存。
 *
 * ``showShortLeverage`` 来自渠道目录；同一账户在各页渠道一致，文本不会因写入侧漂移。
 */
export function writeAccountConfigSummary(
  accountId: number,
  acc: Account,
  { showShortLeverage }: { showShortLeverage: boolean },
): void {
  cache.set(accountId, {
    leverage: describeLeverage(acc.long_leverage, acc.short_leverage, showShortLeverage),
    symbols: describeSymbolControl(acc.forbidden_symbols, acc.risk_symbols),
    algorithm: algorithmScheduleSummary(algorithmRefOf(acc.algorithm), acc.algorithm_schedule?.length ?? 0, Object.keys(acc.product_algorithms ?? {}).length),
    emptyAlgorithm: describeAlgorithmRef(algorithmRefOf(acc.empty_positions_algorithm)),
  })
}
