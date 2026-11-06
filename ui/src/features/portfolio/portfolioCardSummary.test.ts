import { describe, expect, it } from 'bun:test'
import {
  formatTargetWeight,
  formatTargetUpdatedAt,
  portfolioTargetState,
  portfolioTargetSummary,
  targetDirectionClass,
  targetWeightSummary,
} from './portfolioCardSummary'
import type { TargetWeightSnapshot } from '@/types/api'

function snapshot(
  weights: Record<string, number>,
  calculatedAt: string | null = '2026-08-27T09:03:26',
): TargetWeightSnapshot {
  return {
    weights,
    calculated_at: calculatedAt,
    source: 'manual',
    execution_id: null,
    context_account_id: 2,
  }
}

describe('portfolioTargetSummary', () => {
  it('按绝对权重排序并汇总多空敞口', () => {
    const summary = portfolioTargetSummary(snapshot({ small: 0.01, short: -0.3, long: 0.5 }))
    expect(summary.activeCount).toBe(3)
    expect(summary.grossExposure).toBeCloseTo(0.81)
    expect(summary.netExposure).toBeCloseTo(0.21)
    expect(summary.entries).toEqual([
        { symbol: 'long', weight: 0.5 },
        { symbol: 'short', weight: -0.3 },
        { symbol: 'small', weight: 0.01 },
    ])
  })

  it('忽略零值和极小噪声，返回全部活跃品种', () => {
    const summary = portfolioTargetSummary(snapshot({ a: 0.5, b: 0.4, c: 0.3, d: 0.2, e: 0.1, zero: 0, noise: 1e-10 }))
    expect(summary.activeCount).toBe(5)
    expect(summary.entries.map((entry) => entry.symbol)).toEqual(['a', 'b', 'c', 'd', 'e'])
  })

  it('允许总敞口超过 100%', () => {
    expect(portfolioTargetSummary(snapshot({ long: 1.2, short: -0.4 })).grossExposure).toBe(1.6)
  })
})

describe('targetWeightSummary', () => {
  it('为编辑结果汇总品种数量、净敞口和总敞口', () => {
    const summary = targetWeightSummary({ large: 0.8, medium: 0.4, short: -0.2, zero: 0 })
    expect(summary.activeCount).toBe(3)
    expect(summary.netExposure).toBeCloseTo(1)
    expect(summary.grossExposure).toBeCloseTo(1.4)
  })
})

describe('portfolioTargetState', () => {
  it('区分读取中、不可用和从未计算', () => {
    expect(portfolioTargetState(null, true, null, false).kind).toBe('loading')
    expect(portfolioTargetState(null, false, new Error('offline'), false).kind).toBe('unavailable')
    expect(portfolioTargetState(snapshot({}, null), false, null, false).kind).toBe('uncalculated')
  })

  it('区分已计算空仓、正常目标和陈旧缓存', () => {
    expect(portfolioTargetState(snapshot({ zero: 0 }), false, null, false).kind).toBe('empty')
    const state = portfolioTargetState(snapshot({ rb: 0.2 }), false, new Error('refresh failed'), true)
    expect(state.kind).toBe('ready')
    if (state.kind === 'ready') expect(state.stale).toBe(true)
  })
})

describe('formatTargetWeight', () => {
  it('格式化带方向和中性的百分比', () => {
    expect(formatTargetWeight(0.0413)).toBe('+4.1%')
    expect(formatTargetWeight(-0.0268)).toBe('-2.7%')
    expect(formatTargetWeight(0.0001)).toBe('0.0%')
    expect(formatTargetWeight(0.0933, false)).toBe('9.3%')
  })
})

describe('targetDirectionClass', () => {
  it('目标多头红、空头绿，零值与无效值保持中性', () => {
    expect(targetDirectionClass(0.2)).toBe('text-up')
    expect(targetDirectionClass(-0.2)).toBe('text-down')
    expect(targetDirectionClass(0)).toBe('text-ink-1')
    expect(targetDirectionClass(Number.NaN)).toBe('text-ink-1')
  })
})

describe('formatTargetUpdatedAt', () => {
  const now = new Date('2026-08-27T12:00:00+08:00').getTime()

  it('显示今天、昨天和更早的北京时间', () => {
    expect(formatTargetUpdatedAt('2026-08-27T09:03:26', now)).toBe('今天 09:03')
    expect(formatTargetUpdatedAt('2026-08-26T09:32:08', now)).toBe('昨天 09:32')
    expect(formatTargetUpdatedAt('2026-08-24T12:54:32', now)).toBe('8 月 24 日 12:54')
  })

  it('无效时间不伪造日期', () => {
    expect(formatTargetUpdatedAt('invalid', now)).toBe('—')
  })
})


it('数量目标保留小数和极小数量，不汇总跨标的敞口', () => {
  const summary = portfolioTargetSummary({ ...snapshot({}), target_mode: 'quantity',
    raw_targets: { BTCUSDT: 0.015, ETHUSDT: -0.25, SMALL: 0.0000000001 } })
  expect(summary.activeCount).toBe(3)
  expect(summary.mode).toBe('quantity')
  expect(summary.grossExposure).toBe(0)
  expect(summary.netExposure).toBe(0)
  expect(summary.entries.find(row => row.symbol === 'BTCUSDT')).toMatchObject({ weight: 0.015, mode: 'quantity' })
})
