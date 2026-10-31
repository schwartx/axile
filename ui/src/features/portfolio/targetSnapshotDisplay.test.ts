import { describe, expect, it } from 'bun:test'
import { targetSnapshotText } from '@/features/portfolio/targetSnapshotDisplay'
import type { TargetWeightSnapshot } from '@/types/api'

const snapshot: TargetWeightSnapshot = {
  weights: { rb2610: 0.5 },
  calculated_at: '2026-08-25T14:32:18',
  source: 'execution',
  execution_id: 'exec-1',
  context_account_id: 7,
}

describe('targetSnapshotText', () => {
  it('明确区分未计算，而不是误报为空仓', () => {
    expect(targetSnapshotText(null)).toBe('尚无目标权重 · 点击刷新计算')
  })

  it('显示服务端绝对时间、执行来源和账户上下文', () => {
    expect(targetSnapshotText(snapshot, '期货账户')).toBe(
      '目标计算于 2026-08-25 14:32:18 · 执行触发 · 使用账户 期货账户',
    )
  })

  it('无账户上下文时明确标记样例数据', () => {
    expect(targetSnapshotText({ ...snapshot, source: 'manual', context_account_id: null })).toBe(
      '目标计算于 2026-08-25 14:32:18 · 主动刷新 · 样例数据',
    )
  })
})


it('UTC时间按上海时间展示并保留跨日日期', () => {
  expect(targetSnapshotText({ ...snapshot, calculated_at: '2026-01-01T16:30:00.123456Z' })).toContain('2026-01-02 00:30:00')
})
