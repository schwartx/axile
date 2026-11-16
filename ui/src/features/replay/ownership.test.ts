import { expect, it } from 'bun:test'
import { replayOrderOwnershipGroups, replayOwnership } from '@/features/replay/ownership'
import { replayRemainingQuantity } from '@/features/replay/chartGeometry'
import type { ReplayOrder } from '@/features/replay/types'

it('区分本次与其他执行，并保留详情所需执行身份', () => {
  expect(replayOwnership({ execution_id: 'E2', origin: 'execution' }, 'E2')).toEqual({ kind: 'current', label: '本次执行', executionId: 'E2' })
  expect(replayOwnership({ execution_id: 'E1' }, 'E2')).toEqual({ kind: 'other', label: '其他执行', executionId: 'E1' })
})

it('无执行身份时明确人工或外部来源，未知来源不猜成人工', () => {
  expect(replayOwnership({ origin: 'manual' }, 'E2')).toEqual({ kind: 'manual', label: '人工', executionId: null })
  expect(replayOwnership({ execution_id: null, origin: 'external' }, 'E2')).toEqual({ kind: 'external', label: '外部', executionId: null })
  for (const value of [{}, { execution_id: null }, { origin: 'unknown' }, { origin: 'execution' }]) {
    expect(replayOwnership(value, 'E2')).toEqual({ kind: 'unknown', label: '归属未知', executionId: null })
  }
})

it('有效执行身份与人工或外部来源矛盾时归属未知，仍保留原执行ID供详情判断', () => {
  expect(replayOwnership({ execution_id: 'E2', origin: 'manual' }, 'E2')).toEqual({ kind: 'unknown', label: '归属未知', executionId: 'E2' })
  expect(replayOwnership({ execution_id: 'E1', origin: 'external' }, 'E2')).toEqual({ kind: 'unknown', label: '归属未知', executionId: 'E1' })
})

it('原始报撤请求的归属只采用有效字符串和已知字段', () => {
  expect(replayOwnership({ execution_id: 'E2', origin: 'execution', field_quality: { execution_id: 'known' } }, 'E2').kind).toBe('current')
  expect(replayOwnership({ execution_id: 'E1', origin: 'manual', field_quality: { execution_id: 'invalid' } }, 'E2').kind).toBe('manual')
  expect(replayOwnership({ execution_id: 'E2', origin: 'external', field_quality: { origin: 'invalid' } }, 'E2')).toEqual({ kind: 'current', label: '本次执行', executionId: 'E2' })
  expect(replayOwnership({ execution_id: 'E1', origin: 'manual', field_quality: { origin: 'missing' } }, 'E2')).toEqual({ kind: 'other', label: '其他执行', executionId: 'E1' })
  expect(replayOwnership({ execution_id: 'E2', origin: 'manual', field_quality: { execution_id: 'missing', origin: 'invalid' } }, 'E2').kind).toBe('unknown')
  expect(replayOwnership({ execution_id: 2, origin: { source: 'manual' } }, 'E2').kind).toBe('unknown')
})

it('同价订单按归属独立统计，其他执行不会用某一个执行ID冒充全组', () => {
  const orders = [{ execution_id: 'E1', remaining_volume: 3 }, { execution_id: 'E2', remaining_volume: 2 },
    { execution_id: 'E3', remaining_volume: null }, { origin: 'manual', remaining_volume: 5 },
    { origin: 'external', remaining_volume: 6 }, { remaining_volume: 7 }] as ReplayOrder[]
  const groups = replayOrderOwnershipGroups(orders, 'E2')
  expect(groups.map(group => group.kind)).toEqual(['current', 'other', 'manual', 'external', 'unknown'])
  expect(groups.map(group => replayRemainingQuantity(group.orders))).toEqual([2, null, 5, 6, 7])
  expect(groups[1].orders.map(order => order.execution_id)).toEqual(['E1', 'E3'])
  expect('executionId' in groups[1]).toBe(false)
  expect(replayRemainingQuantity(orders)).toBeNull()
})
