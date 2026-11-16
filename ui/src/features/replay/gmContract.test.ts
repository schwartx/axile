import { describe, expect, test } from 'bun:test'
import { projectReplay } from '@/features/replay/model'
import type { ReplayData, ReplayMessage, ReplayWindow } from '@/features/replay/types'
import fixtures from '../../../../tests/fixtures/execution_replay/gm-review-regressions.json'

const key = (side: string) => JSON.stringify(['600000.SH', side])
const message = (capture_seq: number, sample: { topic: string; data: ReplayData }): ReplayMessage => ({
  schema_version: 1, recording_id: 'gm-review', capture_seq, time_us: capture_seq * 100, ...sample,
})
function windowOf(messages: ReplayMessage[], state: ReplayData = {}): ReplayWindow {
  return {
    recording_id: 'gm-review', window_id: 'window-1', context: { schema_version: 1 }, complete: true,
    coverage: [{ start_us: 0, end_us: 10_000 }], gaps: [], next_cursor: null, messages,
    state_before_start: { schema_version: 1, recording_id: 'gm-review', state_through_seq: 0, time_us: 0,
      trade_accounting: { own_seen_keys: [], market_seen_keys: [], own_records: {}, market_records: {},
        own_complete: true, market_complete: true, execution_totals: {} }, ...state },
  }
}

describe('GM 实际适配与前端共用真值', () => {
  test('只有客户端订单号的重复回调仍是一张已确认订单', () => {
    const result = projectReplay(windowOf([message(1, fixtures.order), message(2, fixtures.order)]), { time_us: 200 })
    expect(Object.keys(result.orders)).toEqual([key('one')])
    expect(result.orders[key('one')]).toMatchObject({ order_id: 'one', confirmed: true, remaining_volume: 100 })
  })

  test('原生 side 别名保留多空两项，检查点恢复与完整播放一致', () => {
    const continuous = projectReplay(windowOf([message(1, fixtures.positions)]), { time_us: 100 })
    const positions = Object.fromEntries(fixtures.positions.data.positions.map(row => [key(row.position_side), row]))
    const recovered = projectReplay(windowOf([], { state_through_seq: 1, time_us: 100, observed_positions: positions }), { time_us: 100 })
    expect(continuous.observedPositions[key('LONG')]).toMatchObject({ direction: 'LONG', volume: 100, available_volume: 90, frozen_volume: 10 })
    expect(continuous.observedPositions[key('SHORT')]).toMatchObject({ direction: 'SHORT', volume: 20, available_volume: 15, frozen_volume: 5 })
    expect(Object.keys(continuous.observedPositions)).toHaveLength(2)
    expect(recovered.observedPositions).toEqual(continuous.observedPositions)
  })

  test('撤单执行回报只作命令观察，不污染成交与已知剩余量', () => {
    const result = projectReplay(windowOf([message(1, fixtures.order), message(2, fixtures.cancel)]), { time_us: 200 })
    expect(result.ownTrades).toEqual([])
    expect(result.orders[key('one')].remaining_volume).toBe(100)
    expect(result.accountingComplete).toBe(true)
    expect(result.commands).toHaveLength(1)
  })

  for (const correction of fixtures.corrections) {
    test(`成交更正类型 ${correction.input.exec_type} 沿同一编号保持不确定，不复用旧已知成交量`, () => {
      const messages = [message(1, fixtures.order), message(2, fixtures.fill)]
      const before = projectReplay(windowOf(messages), { time_us: 200 })
      expect(before.orders[key('one')].remaining_volume).toBe(60)
      expect(before.accountingComplete).toBe(true)
      const after = projectReplay(windowOf([...messages, message(3, correction)]), { time_us: 300 })
      expect(after.orders[key('one')].remaining_volume).toBeNull()
      expect(after.accountingComplete).toBe(false)
      expect(after.ownTrades.filter(row => row.key === fixtures.fill.data.trade_key)).toHaveLength(1)
    })
  }
})
