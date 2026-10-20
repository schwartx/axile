import { expect, test } from 'bun:test'
import { aggregateStats, buildEquityPoints } from './derive'
import type { AccountAssetSnapshot, ExecuteRecord } from '@/types/api'

function snapshot(total: number, source: 'execution' | 'manual', createdAt: string): AccountAssetSnapshot {
  return {
    id: null,
    account_id: 1,
    assets: { total_asset: total, currency: 'CNY', positions: [] },
    source,
    execution_id: source === 'execution' ? 'exec-1' : null,
    created_at: createdAt,
  }
}

test('人工刷新快照进入权益曲线，但不增加执行统计', () => {
  const points = buildEquityPoints([
    snapshot(100, 'execution', '2026-08-24T09:30:00'),
    snapshot(108, 'manual', '2026-08-24T10:00:00'),
  ])
  const record: ExecuteRecord = {
    account_id: 1,
    id: 1,
    execution_id: 'exec-1',
    input: { curr_target: {}, last_target: {} },
    result: { account_assets: { total_asset: 100, currency: 'CNY' } },
    is_success: 1,
    created_at: '2026-08-24T09:30:00',
  }

  const stats = aggregateStats([record], points, 'CNY')
  expect(points.map((point) => point.eq)).toEqual([100, 108])
  expect(stats.fills).toBe(1)
  expect(stats.noops).toBe(1)
  expect(stats.pnl).toBe(8)
})

test('休市跳过和部分休市不增加成功或硬失败计数', () => {
  const record = (result: ExecuteRecord['result']): ExecuteRecord => ({ account_id: 1, id: 1, execution_id: 'rest', input: {}, result, is_success: 0, created_at: '2026-10-16T10:15:00' })
  const records = [
    record({ status: 'BLOCKED', task_status: 'SKIPPED', reason_code: 'COMMON.SESSION.CLOSED' }),
    record({ status: 'PARTIAL', reason_code: 'COMMON.SESSION.CLOSED', symbol_results: { m2701: { status: 'BLOCKED', reason_code: 'COMMON.SESSION.CLOSED' }, ag2612: { status: 'NOOP' } } }),
    record({ status: 'FAILED', error: '下单失败' }),
  ]
  expect(aggregateStats(records, [], 'CNY')).toMatchObject({ fills: 0, noops: 0, fails: 1 })
})

test('缺失目标保持未知，显式空目标才计作目标未变', () => {
  const record: ExecuteRecord = {
    account_id: 1, id: 1, execution_id: null, created_at: '2026-10-20T10:00:00', is_success: 1,
    input: null, result: { status: 'SUCCEEDED' },
  }
  expect(aggregateStats([record], []).noops).toBe(0)
  expect(aggregateStats([{ ...record, input: {} }], []).noops).toBe(0)
  expect(aggregateStats([{ ...record, input: { curr_target: {}, last_target: {} } }], []).noops).toBe(1)
})

test('非对象输入保持目标未知，非对象结果仍读取已保存的主记录结论', () => {
  for (const root of [null, '原始根值', 0, false, []]) {
    const record: ExecuteRecord = {
      account_id: 1, id: 1, execution_id: null, created_at: '2026-10-20T10:00:00', is_success: 0,
      input: root, result: root, task_status: 'TERMINATED',
    }
    expect(aggregateStats([record], [])).toMatchObject({ terminated: 1, fails: 0, noops: 0 })
  }
})
