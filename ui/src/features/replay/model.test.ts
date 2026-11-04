import { describe, expect, test } from 'bun:test'
import { createReplayProjector, projectReplay } from '@/features/replay/model'
import type { ReplayData, ReplayMessage, ReplayWindow } from '@/features/replay/types'
import quoteQuality from '../../../../tests/fixtures/execution_replay/quote-quality.json'
import positionMerges from '../../../../tests/fixtures/execution_replay/position-field-merge.json'

const key = (symbol: string, id: string) => JSON.stringify([symbol, id])
const message = (capture_seq: number, topic: string, data: ReplayData, time_us = capture_seq * 100): ReplayMessage => ({
  schema_version: 1, recording_id: 'rec-A', capture_seq, time_us, topic, data,
})
function windowOf(messages: ReplayMessage[], state: ReplayData = {}): ReplayWindow {
  return {
    recording_id: 'rec-A', window_id: 'window-1', context: { schema_version: 1 }, complete: true,
    coverage: [{ start_us: 0, end_us: 10_000 }], gaps: [], next_cursor: null, messages,
    state_before_start: { schema_version: 1, recording_id: 'rec-A', state_through_seq: 0, time_us: 0,
      trade_accounting: { own_seen_keys: [], market_seen_keys: [], own_records: {}, market_records: {}, own_complete: true,
        market_complete: true, execution_totals: {} }, ...state },
  }
}
const order = (id: string, rest: ReplayData = {}): ReplayData => ({ symbol: 'A', order_id: id, direction: 'BUY',
  quantity: 10, filled_quantity: 0, remaining_quantity: 10, price: 98, status: 'working', confirmed_book: true, sync_generation: 1, ...rest })
const fill = (id: string, quantity: number, rest: ReplayData = {}): ReplayData => ({ symbol: 'A', trade_key: id,
  identity_quality: 'stable', quantity, price: 99, direction: 'BUY', position_side: 'NET', execution_id: 'exec-1', ...rest })

describe('增量投影', () => {
  test('前进只解析新增观察，无事件帧不重建历史；后退与完整重建一致', () => {
    let reads = 0
    const data = { get symbol() { reads++; return 'A' }, book_valid: true, available: true, valid_until_us: 1000, bids: [{ price: 98, quantity: 2 }], asks: [] }
    const window = windowOf([message(1, '/quote/A', data), message(2, '/quote/A', data, 300)])
    window.gaps = [{ start_us: 150, end_us: 200, topic: 'depth', reason: 'test gap' }]
    const project = createReplayProjector(window)
    const first = project({ time_us: 100 }), saved = JSON.stringify(first)
    reads = 0
    const idle = project({ time_us: 120 })
    expect(reads).toBe(0)
    expect(idle.quoteHistory).toBe(first.quoteHistory)
    for (const time_us of [160, 210, 300, 1100, 100, 300]) expect(project({ time_us })).toEqual(projectReplay(window, { time_us }))
    expect(JSON.stringify(first)).toBe(saved)
  })

  test('新增稳定成交只更新关联委托，保留其他委托和旧快照', () => {
    const window = windowOf([message(1, '/order', order('O')), message(2, '/order', order('OTHER')),
      message(3, '/trade', fill('T1', 2, { order_id: 'O' })), message(4, '/trade', fill('T2', 3, { order_id: 'O' }))])
    const project = createReplayProjector(window), before = project({ time_us: 300 }), after = project({ time_us: 400 })
    expect(after.orders[key('A', 'O')].filled_volume).toBe(5)
    expect(after.orders[key('A', 'OTHER')]).toBe(before.orders[key('A', 'OTHER')])
    expect(before.orders[key('A', 'O')].filled_volume).toBe(2)
  })

  test('同微秒逐事件、成交补全冲突、命令归属、重复事件和持仓保持完整重建语义', () => {
    const messages = [message(1, '/execution', { execution_id: 'exec-1', phase: 'start', symbols: ['A'] }),
      message(2, '/order', order('O')),
      message(3, '/trade', fill('T1', 2, { order_id: 'O', execution_id: null })),
      message(4, '/command', { command_id: 'C', operation: 'place_order', symbol: 'A', order_id: 'O', execution_id: 'exec-1' }),
      message(5, '/trade', fill('T1', 2, { order_id: 'O' })),
      message(6, '/trade', fill('T2', 3, { order_id: 'O' })),
      message(7, '/trade', fill('T2', 4, { order_id: 'O' })),
      message(8, '/account_observation', { scope: ['A'], positions: [{ symbol: 'A', position_side: 'NET', quantity: 5 }] }),
      message(9, '/trade', { symbol: 'A', quantity: 1, price: 99, order_id: 'O' }),
      message(10, '/trade', fill('T3', 1, { order_id: 'O', related_capture_seq: 9 }))].map(row => ({ ...row, time_us: 100 }))
    messages.splice(6, 0, messages[5])
    const window = windowOf(messages), project = createReplayProjector(window)
    const snapshots: { value: ReturnType<typeof project>; saved: string }[] = []
    for (const capture_seq of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 3, 10]) {
      const cursor = { time_us: 100, capture_seq }, value = project(cursor)
      expect(value).toEqual(projectReplay(window, cursor))
      for (const snapshot of snapshots) expect(JSON.stringify(snapshot.value)).toBe(snapshot.saved)
      snapshots.push({ value, saved: JSON.stringify(value) })
    }
  })
})

describe('回放按本地已观察前缀恢复', () => {
  test('窗口内超过两万条盘口观察仍可回放', () => {
    const events = Array.from({ length: 20_001 }, (_, index) =>
      message(index + 1, '/quote/A', { symbol: 'A', bids: [], asks: [] }, index + 1))
    const result = projectReplay(windowOf(events), { time_us: 20_001 })
    expect(result.quoteHistory).toHaveLength(20_001)
    expect(result.capture_seq).toBe(20_001)
  })

  test('盘口档数来自每次观察，保留未知量和零量；连续播放与窗前恢复一致', () => {
    const depth = { symbol: 'A', book_valid: true, available: true, valid_until_us: 1000,
      bids: [{ price: 98, quantity: 0 }, { price: 97 }, { price: 96, quantity: 8, field_quality: { quantity: 'invalid' } }, { price: 95, quantity: -1 }, { price: 94, quantity: 4, field_quality: { price: 'missing' } }],
      asks: [{ price: 99, quantity: 3 }, { price: 100, quantity: 4 }] }
    const messages = [message(1, '/quote/A', { ...depth, bids: depth.bids.slice(0, 2) }), message(2, '/quote/A', depth)]
    const before = projectReplay(windowOf(messages), { time_us: 100 })
    expect(before.quotes.A.bids).toHaveLength(2)
    const continuous = projectReplay(windowOf(messages), { time_us: 200 })
    const recovered = projectReplay(windowOf([], { state_through_seq: 2, time_us: 200, quotes: { A: { ...depth, observed_at_us: 200 } } }), { time_us: 200 })
    expect(continuous.quotes.A.bids.map(level => level.quantity)).toEqual([0, null, null, null])
    expect(continuous.quotes.A.asks).toHaveLength(2)
    expect(continuous.quotes.A.bids).toEqual(recovered.quotes.A.bids)
    expect(continuous.quotes.A.asks).toEqual(recovered.quotes.A.asks)
  })
  for (const complete of [false, true]) for (const scenario of positionMerges) {
    test(`持仓字段 ${scenario.name} complete=${complete} 连播与窗前/窗后恢复一致`, () => {
      const first = { symbol: 'X', position_side: 'LONG', quantity: 4, ...scenario.initial }
      const second = { symbol: 'X', position_side: 'LONG', quantity: 5, ...scenario.update }
      const merged = { symbol: 'X', position_side: 'LONG', quantity: 5, ...scenario.merged, observed_at_us: 200 }
      const events = [message(1, '/account_observation', { scope: ['X'], scope_complete: true, positions: [first] }),
        message(2, '/account_observation', { scope: ['X'], scope_complete: complete, positions: [second] })]
      const continuous = projectReplay(windowOf(events), { time_us: 200 })
      const before = projectReplay(windowOf([events[1]], { state_through_seq: 1, time_us: 100,
        observed_positions: { [key('X', 'LONG')]: { ...first, observed_at_us: 100 } } }), { time_us: 200 })
      const after = projectReplay(windowOf([], { state_through_seq: 2, time_us: 200,
        observed_positions: { [key('X', 'LONG')]: merged } }), { time_us: 200 })
      expect(continuous.observedPositions[key('X', 'LONG')]).toMatchObject({ volume: 5, today_volume: scenario.visible })
      expect(continuous.observedPositions).toEqual(before.observedPositions)
      expect(continuous.observedPositions).toEqual(after.observedPositions)
      expect(continuous.derivedPositions).toEqual({})
    })
  }

  test('完整范围缺席将曾记录但质量未知的字段确认为零，未记录字段仍未知', () => {
    const result = projectReplay(windowOf([
      message(1, '/account_observation', { scope: ['X'], scope_complete: false,
        positions: [{ symbol: 'X', position_side: 'LONG', quantity: null, today_quantity: 2,
          field_quality: { quantity: 'missing', today_quantity: 'invalid' } }] }),
      message(2, '/account_observation', { scope: ['X'], scope_complete: true, positions: [] }),
    ]), { time_us: 200 })
    expect(result.observedPositions[key('X', 'LONG')]).toMatchObject({ volume: 0, today_volume: 0, available_volume: null })
  })

  test('恢复窗前计量后补查同笔不重计，新增稳定成交才增加', () => {
    const state = {
      state_through_seq: 2,
      trade_accounting: { own_seen_keys: ['T1'], market_seen_keys: ['M1'], own_complete: true, market_complete: true,
        own_records: { T1: { data: fill('T1', 4), first_time_us: 100, first_capture_seq: 1, conflict: false } }, market_records: {},
        execution_totals: { 'exec-1': { complete: true, by_symbol: { A: { quantity: 4, notional: 396, complete: true } } } } },
    }
    const window = windowOf([message(3, '/trade', fill('T1', 4)), message(4, '/trade', fill('T2', 2)),
      message(4, '/trade', fill('T2', 2))], state)
    expect(projectReplay(window, { time_us: 300 }).executionTotals['exec-1'].A).toBe(4)
    const result = projectReplay(window, { time_us: 400 })
    expect(result.executionTotals['exec-1'].A).toBe(6)
    expect(result.ownTrades.map(item => item.key)).toEqual(['T1', 'T2'])
    expect(projectReplay(window, { time_us: 300 }).executionTotals['exec-1'].A).toBe(4)
  })

  test('观察持仓已含迟到成交时不盲加，只有业务proof对齐才推导', () => {
    const observation = { scope: { symbols: ['A'] }, scope_complete: true,
      positions: [{ symbol: 'A', position_side: 'NET', quantity: 4, available_quantity: 1 }],
      position_basis: { status: 'unverified', proof: null } }
    const result = projectReplay(windowOf([message(1, '/account_observation', observation), message(2, '/trade', fill('T1', 4))]), { time_us: 200 })
    expect(Object.values(result.observedPositions)[0].volume).toBe(4)
    expect(result.derivedPositions).toEqual({})
    const aligned = { ...observation, position_basis: { status: 'aligned', proof: { kind: 'included_trade_keys', complete: true, included_trade_keys: ['T1'] } } }
    const next = projectReplay(windowOf([message(1, '/account_observation', aligned), message(2, '/trade', fill('T1', 4)),
      message(3, '/trade', fill('T2', 2))]), { time_us: 300 })
    expect(Object.values(next.derivedPositions)[0].volume).toBe(6)
    expect(Object.values(next.observedPositions)[0].available_volume).toBe(1)
  })

  test('局部账户观察缺行不清其他品种，partial缺字段不改原生可卖', () => {
    const first = message(1, '/account_observation', { scope: 'account', scope_complete: true,
      positions: [{ symbol: 'A', position_side: 'LONG', quantity: 4, available_quantity: 2 },
        { symbol: 'B', position_side: 'LONG', quantity: 8 }] })
    const second = message(2, '/account_observation', { scope: { symbols: ['A'] }, scope_complete: false,
      positions: [{ symbol: 'A', position_side: 'LONG', quantity: 5 }] })
    const third = message(3, '/account_observation', { scope: { symbols: ['A'] }, scope_complete: true, positions: [] })
    const atTwo = projectReplay(windowOf([first, second, third]), { time_us: 200 })
    expect(atTwo.observedPositions[key('A', 'LONG')].available_volume).toBe(2)
    const after = projectReplay(windowOf([first, second, third]), { time_us: 300 })
    expect(after.observedPositions[key('A', 'LONG')].volume).toBe(0)
    expect(after.observedPositions[key('B', 'LONG')].volume).toBe(8)
  })

  test('请求不当受理，弱身份及有损默认不进入精确计量', () => {
    const result = projectReplay(windowOf([
      message(1, '/command', { command_id: 'c1', operation: 'place_order', phase: 'request', symbol: 'A', price: 98, quantity: 10 }),
      message(2, '/order', order('x', { confirmed_book: null, direction: 'BUY', field_quality: { direction: 'missing' } })),
      message(3, '/trade', { ...fill('weak', 4), identity_quality: 'weak', trade_key: null }),
    ]), { time_us: 300 })
    expect(result.commands.length).toBe(1)
    expect(result.orders[key('A', 'x')].confirmed).toBe(false)
    expect(result.orders[key('A', 'x')].direction).toBeNull()
    expect(result.executionTotals).toEqual({})
    expect(result.accountingComplete).toBe(false)
  })

  test('同微秒按seq步进，倒退不泄露未来订单与热力，录制段不可混入', () => {
    const quote = message(1, '/quote/A', { symbol: 'A', bids: [{ price: 98, quantity: 2 }], asks: [{ price: 99, quantity: 3 }],
      book_valid: true, available: true, valid_until_us: 1000 }, 100)
    const window = windowOf([quote, message(2, '/order', order('x'), 100)])
    const before = projectReplay(window, { time_us: 100, capture_seq: 1 })
    expect(before.orders).toEqual({})
    expect(before.quoteHistory.length).toBe(1)
    expect(projectReplay(window, { time_us: 100, capture_seq: 2 }).orderHistory.length).toBe(1)
    expect(projectReplay(window, { time_us: 99 }).quoteHistory).toEqual([])
    const mixed = { ...window, messages: [{ ...quote, recording_id: 'rec-B' }] }
    expect(() => projectReplay(mixed, { time_us: 100 })).toThrow('其他录制段')
  })

  test('完整集合只清覆盖前缀，较旧查询不回退并保留查询期间的部分成交', () => {
    const messages = [message(1, '/order', order('a', { sync_generation: 1 })), message(2, '/order', order('b', { sync_generation: 1 })),
      message(3, '/order', order('b', { sync_generation: 1, filled_quantity: 6, remaining_quantity: 4, status: 'partially_filled' })),
      message(4, '/order_set', { scope: { symbols: ['A'] }, scope_complete: true, synchronized: true, sync_generation: 1,
        query_basis: { start_seq: 2, request_revision: 2 }, orders: [order('b')] }),
      message(5, '/order_set', { scope: { symbols: ['A'] }, scope_complete: true, synchronized: true, sync_generation: 1,
        query_basis: { start_seq: 2, request_revision: 1 }, orders: [] })]
    const result = projectReplay(windowOf(messages), { time_us: 500 })
    expect(result.orders[key('A', 'a')].confirmed).toBe(false)
    expect(result.orders[key('A', 'a')].status).toBe('unknown')
    expect(result.orders[key('A', 'b')].confirmed).toBe(true)
    expect(result.orders[key('A', 'b')].remaining_volume).toBe(4)
    expect(result.orderHistory.some(row => row.order_id === 'a' && !row.confirmed)).toBe(true)
  })

  test('单笔旧查询不覆盖同代次WS改价改量，检查点恢复与完整前缀一致', () => {
    const first = order('a', { price: 100 }), other = order('b', { price: 80 })
    const amended = order('a', { price: 101, quantity: 12, remaining_quantity: 12, attributes: { source: 'ws' } })
    const stale = message(4, '/order', order('a', { price: 100, attributes: { source: 'rest' },
      query_basis: { recording_id: 'rec-A', start_seq: 2, request_revision: 1 } }))
    const full = windowOf([message(1, '/order', first), message(2, '/order', other), message(3, '/order', amended), stale])
    const restored = windowOf([stale], { state_through_seq: 3, time_us: 300, sync_generation: { A: 1 },
      order_change_seq: { [key('A', 'a')]: 3, [key('A', 'b')]: 2 },
      orders: { [key('A', 'a')]: { ...amended, observed_seq: 3, observed_at_us: 300 },
        [key('A', 'b')]: { ...other, observed_seq: 2, observed_at_us: 200 } } })
    for (const window of [full, restored]) {
      const result = projectReplay(window, { time_us: 400 })
      expect(result.orders[key('A', 'a')]).toMatchObject({ price: 101, volume: 12, remaining_volume: 12,
        confirmed: true, capture_seq: 3, attributes: { source: 'ws' } })
      expect(result.orders[key('A', 'b')]).toMatchObject({ price: 80, confirmed: true })
      expect(result.orderHistory.some(value => value.capture_seq === 4)).toBe(false)
      const fresh = message(5, '/order', order('a', { price: 102,
        query_basis: { recording_id: 'rec-A', start_seq: 4, request_revision: 2 } }))
      expect(projectReplay({ ...window, messages: [...window.messages, fresh] }, { time_us: 500 }).orders[key('A', 'a')])
        .toMatchObject({ price: 102, confirmed: true, capture_seq: 5 })
    }
  })

  test.each([
    null, [], {}, { recording_id: 'rec-B', start_seq: 1, request_revision: 1 },
    { start_seq: 1, request_revision: 1 },
    { recording_id: 'rec-A', start_seq: -1, request_revision: 1 },
    { recording_id: 'rec-A', start_seq: 3, request_revision: 1 },
    { recording_id: 'rec-A', start_seq: 4, request_revision: 1 },
    { recording_id: 'rec-A', start_seq: 1, request_revision: 0 },
    { recording_id: 'rec-A', start_seq: 1, request_revision: Number.MAX_SAFE_INTEGER + 1 },
    { recording_id: 'rec-A', start_seq: 1, request_revision: 1, sync_generation: 1 },
  ].map(query_basis => [query_basis] as const))('明确无效单笔query证明整行忽略，不推进代次或制造确认：%j', query_basis => {
    const invalid = message(3, '/order', order('a', { price: 999, sync_generation: 2, query_basis }))
    const prefix = [message(1, '/order', order('a', { price: 100 })), message(2, '/order', order('a', { price: 101 }))]
    const after = projectReplay(windowOf([...prefix, invalid]), { time_us: 300 })
    expect(after.orders[key('A', 'a')]).toMatchObject({ price: 101, confirmed: true, capture_seq: 2 })
    expect(projectReplay(windowOf([{ ...invalid, data: { ...invalid.data, order_id: 'new' } }]), { time_us: 300 }).orders).toEqual({})
    const next = projectReplay(windowOf([...prefix, invalid, message(4, '/order', order('a', { price: 102 }))]), { time_us: 400 })
    expect(next.orders[key('A', 'a')]).toMatchObject({ price: 102, confirmed: true, capture_seq: 4 })
  })

  test('单笔合法查询按本单水位合并且保留其它单，无query key的实时观察继续生效', () => {
    const query = order('a', { price: 101, query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 1 } })
    const messages = [message(1, '/order', order('a', { price: 100 })),
      message(2, '/order', order('b', { price: 80 })), message(3, '/order', query)]
    const result = projectReplay(windowOf(messages), { time_us: 300 })
    expect(result.orders[key('A', 'a')]).toMatchObject({ price: 101, confirmed: true, capture_seq: 3 })
    expect(result.orders[key('A', 'b')]).toMatchObject({ price: 80, confirmed: true })
    expect(projectReplay(windowOf([...messages, message(4, '/order', order('a', { price: 103 }))]), { time_us: 400 })
      .orders[key('A', 'a')]).toMatchObject({ price: 103, confirmed: true })
    expect(projectReplay(windowOf([message(2, '/order', { ...query, sync_generation: null })]), { time_us: 200 }).orders).toEqual({})
    expect(projectReplay(windowOf([message(2, '/order', { ...query,
      query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 1, sync_generation: 1 } })]), { time_us: 200 }).orders[key('A', 'a')])
      .toMatchObject({ price: 101, confirmed: true })
  })

  test.each([false, true])('并发单笔查询先返回者形成变化水位，晚返回的旧起点保守跳过：newerFirst=%s', newerFirst => {
    const query = (price: number, request_revision: number, start_seq = 1) => order('a', { price,
      query_basis: { recording_id: 'rec-A', start_seq, request_revision } })
    const first = query(newerFirst ? 102 : 101, newerFirst ? 2 : 1)
    const later = query(newerFirst ? 101 : 102, newerFirst ? 1 : 2)
    const messages = [message(1, '/order', order('a', { price: 100 })), message(2, '/order', first), message(3, '/order', later)]
    expect(projectReplay(windowOf(messages), { time_us: 300 }).orders[key('A', 'a')])
      .toMatchObject({ price: first.price, confirmed: true, capture_seq: 2 })
    expect(projectReplay(windowOf([...messages, message(4, '/order', query(104, 3, 3))]), { time_us: 400 }).orders[key('A', 'a')])
      .toMatchObject({ price: 104, confirmed: true, capture_seq: 4 })
  })

  test.each([[true, 1, 2], [false, 1, 2], [true, 2, 1], [false, 2, 1]] as const)(
    '完整集合交付后挡住较早开始的单笔查询，present=%s singleRev=%i setRev=%i', (present, singleRevision, setRevision) => {
      const initial = order('a', { price: 100 })
      const complete = message(2, '/order_set', { scope: ['A'], scope_complete: true, synchronized: true, sync_generation: 1,
        query_basis: { start_seq: 1, request_revision: setRevision }, orders: present ? [order('a', { price: 101 })] : [] })
      const stale = message(3, '/order', order('a', { price: 100,
        query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: singleRevision } }))
      const restored = windowOf([stale], { state_through_seq: 2, time_us: 200, sync_generation: { A: 1 },
        last_set_revision: { A: setRevision }, last_set_observation: { A: [1, setRevision, 2] },
        order_change_seq: { [key('A', 'a')]: 1 }, orders: { [key('A', 'a')]: { ...initial,
          price: present ? 101 : 100, confirmed_book: present, confirmation_state: present ? 'confirmed' : 'absent',
          observed_seq: present ? 2 : 1, observed_at_us: present ? 200 : 100 } } })
      for (const window of [windowOf([message(1, '/order', initial), complete, stale]), restored]) {
        const value = projectReplay(window, { time_us: 300 }).orders[key('A', 'a')]
        expect(value).toMatchObject({ price: present ? 101 : 100, confirmed: present, status: present ? 'working' : 'unknown' })
        expect(projectReplay({ ...window, messages: window.messages.map(value => value.capture_seq === 3
          ? { ...value, data: { ...value.data, order_id: 'new' } } : value) }, { time_us: 300 }).orders[key('A', 'new')]).toBeUndefined()
        const fresh = message(4, '/order', order('a', { price: 103,
          query_basis: { recording_id: 'rec-A', start_seq: 3, request_revision: 3 } }))
        expect(projectReplay({ ...window, messages: [...window.messages, fresh] }, { time_us: 400 }).orders[key('A', 'a')])
          .toMatchObject({ price: 103, confirmed: true })
        expect(projectReplay({ ...window, messages: [...window.messages, message(4, '/order', order('a', { price: 104 }))] }, { time_us: 400 })
          .orders[key('A', 'a')]).toMatchObject({ price: 104, confirmed: true })
      }
    })

  test('完整空账户集合也保护尚未发现的品种，原生检查点空串水位与全播一致', () => {
    const complete = message(1, '/order_set', { scope: { kind: 'account', symbols: [] }, scope_complete: true,
      synchronized: true, sync_generation: 2, query_basis: { start_seq: 0, request_revision: 1 }, orders: [] })
    const baseline = { state_through_seq: 1, time_us: 100, last_set_observation: { '': [2, 1, 1] } }
    for (const window of [windowOf([complete]), windowOf([], baseline)]) {
      const single = (generation: number, start_seq: number) => message(2, '/order', order('new', { symbol: 'X', sync_generation: generation,
        query_basis: { recording_id: 'rec-A', start_seq, request_revision: 2 } }))
      for (const query of [single(2, 0), single(1, 1)]) expect(projectReplay({ ...window, messages: [...window.messages, query] }, { time_us: 200 }).orders).toEqual({})
      for (const query of [single(2, 1), single(3, 0)]) expect(projectReplay({ ...window, messages: [...window.messages, query] }, { time_us: 200 })
        .orders[key('X', 'new')]).toMatchObject({ confirmed: true })
    }
  })

  test.each([false, true])('账户完整集合在部分品种已过期时不推进global，仍局部处理有效品种：otherKnown=%s', otherKnown => {
    const other = otherKnown ? 'C' : 'B'
    const initial = [message(1, '/order', order('a', { price: 100 })),
      message(2, '/order', order(otherKnown ? 'b' : 'a', { symbol: otherKnown ? 'B' : 'A', price: otherKnown ? 200 : 100 }))]
    const scoped = message(3, '/order_set', { scope: ['A'], scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { start_seq: 2, request_revision: 2 }, orders: [order('a', { price: 101 })] })
    const staleAccount = message(4, '/order_set', { scope: { kind: 'account', symbols: [] }, scope_complete: true,
      synchronized: true, sync_generation: 1, query_basis: { start_seq: 2, request_revision: 1 }, orders: [] })
    const concurrent = message(5, '/order', order('new', { symbol: other,
      query_basis: { recording_id: 'rec-A', start_seq: 2, request_revision: 3 } }))
    const full = windowOf([...initial, scoped, staleAccount, concurrent])
    const baseline = { state_through_seq: 4, time_us: 400, sync_generation: { A: 1, ...(otherKnown ? { B: 1 } : {}) },
      last_set_revision: { A: 2, ...(otherKnown ? { B: 1 } : {}) },
      last_set_observation: { A: [1, 2, 3], ...(otherKnown ? { B: [1, 1, 4] } : {}) },
      order_change_seq: { [key('A', 'a')]: otherKnown ? 1 : 2, ...(otherKnown ? { [key('B', 'b')]: 2 } : {}) },
      orders: { [key('A', 'a')]: order('a', { price: 101, observed_seq: 3, observed_at_us: 300 }),
        ...(otherKnown ? { [key('B', 'b')]: order('b', { symbol: 'B', price: 200, observed_seq: 2,
          observed_at_us: 200, confirmed_book: false, confirmation_state: 'absent' }) } : {}) } }
    for (const window of [full, windowOf([concurrent], baseline)]) {
      const result = projectReplay(window, { time_us: 500 })
      expect(result.orders[key('A', 'a')]).toMatchObject({ price: 101, confirmed: true })
      expect(result.orders[key(other, 'new')]).toMatchObject({ confirmed: true })
      if (otherKnown) expect(result.orders[key('B', 'b')]).toMatchObject({ status: 'unknown', confirmed: false })
    }
  })

  test('品种集合只保护其范围，空品种不能冒充账户水位', () => {
    const complete = (scope: unknown) => message(2, '/order_set', { scope, scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { start_seq: 1, request_revision: 1 }, orders: [] })
    const single = (symbol: string) => message(3, '/order', order('new', { symbol,
      query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 2 } }))
    expect(projectReplay(windowOf([complete(['A']), single('B')]), { time_us: 300 }).orders[key('B', 'new')]).toMatchObject({ confirmed: true })
    expect(projectReplay(windowOf([complete(['A']), single('A')]), { time_us: 300 }).orders).toEqual({})
    expect(projectReplay(windowOf([complete(['']), single('B')]), { time_us: 300 }).orders[key('B', 'new')]).toMatchObject({ confirmed: true })
  })

  test.each([
    { scope_complete: false }, { synchronized: false }, { query_basis: null },
    { query_basis: { recording_id: 'wrong', start_seq: 2, request_revision: 6 } },
    { sync_generation: 0 }, { query_basis: { start_seq: 2, request_revision: 5 } },
    { query_basis: { start_seq: 2, request_revision: 4 } },
  ])('无效、部分、旧代或过期完整集合不推进交付水位：%j', invalid => {
    for (const account of [false, true]) {
      const scope = account ? '' : 'A'
      const state = { state_through_seq: 2, time_us: 200, sync_generation: { A: 1 }, last_set_revision: { A: 5 },
        last_set_observation: { [scope]: [1, 5, 2] } }
      const complete = message(3, '/order_set', { scope: account ? { kind: 'account', symbols: [] } : ['A'], scope_complete: true,
        synchronized: true, sync_generation: 1, query_basis: { start_seq: 2, request_revision: 6 }, orders: [], ...invalid })
      const fresh = message(4, '/order', order('new', { price: 104,
        query_basis: { recording_id: 'rec-A', start_seq: 2, request_revision: 7 } }))
      expect(projectReplay(windowOf([complete, fresh], state), { time_us: 400 }).orders[key('A', 'new')])
        .toMatchObject({ price: 104, confirmed: true })
    }
  })

  test.each([false, true])('新代次较低revision完整集合仍建立本代水位，account=%s', account => {
    const scope = account ? '' : 'A'
    const baseline = { state_through_seq: 2, time_us: 200, sync_generation: { A: 1 }, last_set_revision: { A: 99 },
      last_set_observation: { [scope]: [1, 99, 2] } }
    const complete = message(3, '/order_set', { scope: account ? { kind: 'account', symbols: ['A'] } : ['A'], scope_complete: true,
      synchronized: true, sync_generation: 2, query_basis: { start_seq: 2, request_revision: 1 }, orders: [] })
    const single = message(4, '/order', order('new', { sync_generation: 2,
      query_basis: { recording_id: 'rec-A', start_seq: 2, request_revision: 2 } }))
    expect(projectReplay(windowOf([complete, single], baseline), { time_us: 400 }).orders).toEqual({})
    expect(projectReplay(windowOf([complete, { ...single, data: { ...single.data,
      query_basis: { recording_id: 'rec-A', start_seq: 3, request_revision: 2 } } }], baseline), { time_us: 400 }).orders[key('A', 'new')])
      .toMatchObject({ confirmed: true })
  })

  test('窄范围失效只清本品种水位，保留其它品种及账户水位', () => {
    const loss = message(3, '/connection', { scope: ['A'], connected: false, sync_generation: 1 })
    const single = (symbol: string) => message(4, '/order', order('new', { symbol,
      query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 2 } }))
    const baseline = { state_through_seq: 2, time_us: 200, sync_generation: { A: 1, B: 1 },
      last_set_observation: { A: [1, 1, 2], B: [1, 1, 2] } }
    expect(projectReplay(windowOf([loss, single('A')], baseline), { time_us: 400 }).orders[key('A', 'new')]).toMatchObject({ confirmed: true })
    expect(projectReplay(windowOf([loss, single('B')], baseline), { time_us: 400 }).orders).toEqual({})
    expect(projectReplay(windowOf([loss, single('A')], { ...baseline,
      last_set_observation: { ...baseline.last_set_observation, '': [1, 1, 2] } }), { time_us: 400 }).orders).toEqual({})
    const healthy = { ...loss, data: { ...loss.data, connected: true } }
    expect(projectReplay(windowOf([healthy, single('A')], baseline), { time_us: 400 }).orders).toEqual({})
  })

  test.each([null, 0, 1])('账户失效只有明确当前代次才清账户和品种水位：generation=%s', generation => {
    const baseline = { state_through_seq: 2, time_us: 200, sync_generation: { A: 1 },
      last_set_observation: { '': [1, 1, 2], A: [1, 1, 2] } }
    const loss = message(3, '/connection', { scope: { kind: 'account', symbols: [] }, connected: false, sync_generation: generation })
    const single = message(4, '/order', order('new', { symbol: 'X',
      query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 2 } }))
    const result = projectReplay(windowOf([loss, single], baseline), { time_us: 400 })
    if (generation === 1) expect(result.orders[key('X', 'new')]).toMatchObject({ confirmed: true })
    else expect(result.orders).toEqual({})
  })

  test.each([false, true])('旧代次失效不能清较高代次水位，即使品种此前尚未发现：account=%s', account => {
    const baseline = { state_through_seq: 2, time_us: 200, last_set_observation: { [account ? '' : 'A']: [2, 1, 2] } }
    const loss = message(3, '/connection', { scope: account ? { kind: 'account', symbols: [] } : ['A'], connected: false, sync_generation: 1 })
    const single = message(4, '/order', order('new', { sync_generation: 2,
      query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 2 } }))
    expect(projectReplay(windowOf([loss, single], baseline), { time_us: 400 }).orders).toEqual({})
  })

  test('旧检查点未提供集合交付水位时仍可恢复并接受后发查询', () => {
    const state = { state_through_seq: 2, time_us: 200, sync_generation: { A: 1 }, last_set_revision: { A: 1 } }
    const single = message(3, '/order', order('new', { query_basis: { recording_id: 'rec-A', start_seq: 2, request_revision: 2 } }))
    expect(projectReplay(windowOf([single], state), { time_us: 300 }).orders[key('A', 'new')]).toMatchObject({ confirmed: true })
  })

  test('待撤仍在场，断线先失效，旧代回报不复活', () => {
    const result = projectReplay(windowOf([message(1, '/quote/A', { symbol: 'A', bids: [{ price: 98, quantity: 2 }], asks: [], book_valid: true, available: true, valid_until_us: 5000 }, 50),
      message(2, '/order', order('a', { sync_generation: 1 }), 100),
      message(3, '/command', { command_id: 'cancel-a', operation: 'cancel_order', phase: 'request', symbol: 'A', order_id: 'a' }, 200),
      message(4, '/connection', { connected: false, scope: { symbols: ['A'] }, sync_generation: 2 }, 300),
      message(5, '/order', order('a', { sync_generation: 1 }), 400),
    ]), { time_us: 400 })
    expect(result.orderHistory[1].cancel_pending).toBe(true)
    expect(result.orderHistory[1].confirmed).toBe(true)
    expect(result.orders[key('A', 'a')].confirmed).toBe(false)
    expect(result.quoteHistory.at(-1)?.available).toBe(false)
    expect(result.quoteHistory.at(-1)?.time_us).toBe(300)
  })

  test('真实checkpoint注解和多品种阶段目标恢复，不用账户历史完整性掩盖单次执行', () => {
    const state = {
      state_through_seq: 3, time_us: 300,
      executions: { E: { start_observed: true, symbols: ['A', 'B'], by_symbol: {
        A: { target_quantity: 10 }, B: { target_quantity: -2 },
      } } },
      orders: { [key('A', 'a')]: { ...order('a'), observed_seq: 2, observed_at_us: 200 } },
      trade_accounting: { own_complete: false, market_complete: false, own_seen_keys: ['T'], market_seen_keys: [], pending: {},
        own_records: { T: { data: fill('T', 4, { execution_id: 'E', order_id: 'a' }), first_capture_seq: 3, first_time_us: 300,
          last_observed_seq: 3, last_observed_time_us: 300, conflict: false } }, market_records: {}, execution_totals: {} },
    }
    const result = projectReplay(windowOf([message(4, '/trade', fill('T', 4, { execution_id: 'E', order_id: 'a' }))], state), { time_us: 400 })
    expect(result.targets).toEqual({ A: 10, B: -2 })
    expect(result.orders[key('A', 'a')].time_us).toBe(200)
    expect(result.orders[key('A', 'a')].carry_in).toBe(true)
    expect(result.executionTotals.E).toEqual({ A: 4, B: 0 })
    expect(result.executionComplete.E).toBe(true)
    expect(result.accountingComplete).toBe(false)
    expect(result.ownTrades.length).toBe(1)
  })

  test('源业务水位只排除同来源已包含成交，phase目标不证明执行起点', () => {
    const positions = [{ symbol: 'A', position_side: 'NET', quantity: 4 }]
    const messages = [
      message(1, '/execution', { execution_id: 'E', event: 'phase', symbol: 'A', target_quantity: 10 }),
      message(2, '/account_observation', { scope: ['A'], scope_complete: true, positions,
        position_basis: { status: 'aligned', proof: { kind: 'source_watermark', source: 'source-1', through: 5 } } }),
      message(3, '/trade', fill('T', 4, { source: 'source-1', source_seq: 5, execution_id: 'E', order_id: 'a' })),
      message(4, '/trade', fill('U', 2, { source: 'source-1', source_seq: 6, execution_id: 'E', order_id: 'a' })),
    ]
    const result = projectReplay(windowOf(messages), { time_us: 400 })
    expect(result.executionComplete.E).toBe(false)
    expect(Object.values(result.derivedPositions)[0].volume).toBe(6)
    expect(result.targets.A).toBe(10)
  })

  test('账户scope即使symbols为空也覆盖已知订单盘口及持仓', () => {
    const account = { kind: 'account', symbols: [] }
    const messages = [message(1, '/quote/A', { symbol: 'A', book_valid: true, available: true, valid_until_us: 10_000,
      bids: [{ price: 98, quantity: 5 }], asks: [] }), message(2, '/order', order('a')),
    message(3, '/account_observation', { scope: account, scope_complete: true,
      positions: [{ symbol: 'B', position_side: 'LONG', quantity: 3, available_quantity: 2, frozen_quantity: 1 }] }),
    message(4, '/account_observation', { scope: account, scope_complete: true, positions: [] }),
    message(5, '/order_set', { scope: account, scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { start_seq: 4, request_revision: 1 }, orders: [] }),
    message(6, '/connection', { scope: account, sync_generation: 2, connected: false })]
    const result = projectReplay(windowOf(messages), { time_us: 600 })
    expect(result.observedPositions[key('B', 'LONG')]).toMatchObject({ volume: 0, available_volume: 0, frozen_volume: 0 })
    expect(result.orders[key('A', 'a')].confirmed).toBe(false)
    expect(result.quotes.A.available).toBe(false)
    expect(result.quoteHistory.at(-1)?.time_us).toBe(600)
  })

  test('阶段start建立单次计量，各执行和品种目标独立，未知A不遮掉有效B', () => {
    const messages = [message(1, '/execution', { execution_id: 'E', phase: 'start', symbols: ['A', 'B'],
      by_symbol: { A: { target_quantity: 10, algorithm: 'CUSTOM' }, B: { target_quantity: 20 } } }),
    message(2, '/execution', { execution_id: 'E', phase: 'target', symbol: 'B', target_quantity: 18 }),
    message(3, '/trade', fill('B1', 2, { symbol: 'B', execution_id: 'E', order_id: 'b' })),
    message(4, '/trade', fill('A-weak', 4, { execution_id: 'E', identity_quality: 'weak', trade_key: null })),
    message(5, '/execution', { execution_id: 'F', phase: 'target', symbol: 'A', target_quantity: 99 })]
    const result = projectReplay(windowOf(messages), { time_us: 500 })
    expect(result.targetsByExecution.E).toEqual({ A: 10, B: 18 })
    expect(result.targetsByExecution.F).toEqual({ A: 99 })
    expect(result.executionSymbolComplete.E).toEqual({ A: false, B: true })
    expect(result.executionComplete.E).toBe(false)
    expect(result.executionTotals.E.B).toBe(2)
  })

  test('窗前weak观察可见且不计量，明确关联清原生pending键而非猜去重', () => {
    const state = { state_through_seq: 2, time_us: 200,
      executions: { E: { start_observed: true, symbols: ['A'] } },
      trade_accounting: { own_complete: false, market_complete: false, own_seen_keys: [], market_seen_keys: [],
        own_records: {}, market_records: {}, pending: { 'own:observation:2': { market: false,
          observation: { ...fill('ignored', 4, { identity_quality: 'weak', trade_key: null, execution_id: 'E' }), observed_seq: 2, observed_at_us: 200 } } } } }
    const window = windowOf([message(3, '/trade', fill('T', 4, { related_capture_seq: 2, execution_id: 'E', order_id: 'a' }))], state)
    const before = projectReplay(window, { time_us: 200 })
    expect(before.ownTrades).toHaveLength(1)
    expect(before.ownTrades[0].key).toBeNull()
    expect(before.executionSymbolComplete.E.A).toBe(false)
    const after = projectReplay(window, { time_us: 300 })
    expect(after.ownTrades.map(row => row.key)).toEqual(['T'])
    expect(after.executionTotals.E.A).toBe(4)
    expect(after.executionSymbolComplete.E.A).toBe(true)
  })

  test('独立成交只取柜台累计与已去重逐笔的max，弱成交使剩余未知', () => {
    const messages = [message(1, '/order', order('a')),
      message(2, '/trade', fill('T', 4, { order_id: 'a' })),
      message(3, '/order', order('a', { status: 'partially_filled', filled_quantity: 4, remaining_quantity: 6 })),
      message(4, '/trade', fill('T', 4, { order_id: 'a' })),
      message(5, '/trade', fill('U', 2, { order_id: 'a' })),
      message(6, '/trade', fill('weak', 2, { trade_key: null, identity_quality: 'weak', order_id: 'a' }))]
    const window = windowOf(messages)
    expect(projectReplay(window, { time_us: 200 }).orders[key('A', 'a')]).toMatchObject({ filled_volume: 4, remaining_volume: 6 })
    expect(projectReplay(window, { time_us: 400 }).orders[key('A', 'a')]).toMatchObject({ filled_volume: 4, remaining_volume: 6 })
    expect(projectReplay(window, { time_us: 500 }).orders[key('A', 'a')]).toMatchObject({ filled_volume: 6, remaining_volume: 4 })
    expect(projectReplay(window, { time_us: 600 }).orders[key('A', 'a')].remaining_volume).toBeNull()
  })

  test('重复观察不保护旧集合，新成交事实才保护查询期间变化', () => {
    const first = [message(1, '/order', order('a')), message(2, '/trade', fill('T', 4, { order_id: 'a' }))]
    const query = { scope: ['A'], scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { start_seq: 2, request_revision: 1 }, orders: [] }
    const duplicate = projectReplay(windowOf([...first, message(3, '/trade', fill('T', 4, { order_id: 'a' })), message(4, '/order_set', query)]), { time_us: 400 })
    expect(duplicate.orders[key('A', 'a')].confirmed).toBe(false)
    const newFact = projectReplay(windowOf([...first, message(3, '/trade', fill('U', 2, { order_id: 'a' })), message(4, '/order_set', query)]), { time_us: 400 })
    expect(newFact.orders[key('A', 'a')].confirmed).toBe(true)
    expect(newFact.orders[key('A', 'a')].remaining_volume).toBe(4)
  })

  test('partial订单保留确认和已知字段，终态及累计回退只保护相应字段', () => {
    const messages = [message(1, '/order', order('a', { attributes: { original: '保留' } })),
      message(2, '/order', { symbol: 'A', order_id: 'a', sync_generation: 1, price: 99, attributes: { later: false } }),
      message(3, '/order', order('a', { status: 'filled', filled_quantity: 10, remaining_quantity: 0 })),
      message(4, '/order', order('a', { status: 'working', filled_quantity: 1, remaining_quantity: 9, price: 101, attributes: { reason: '迟到补充' } }))]
    const window = windowOf(messages)
    expect(projectReplay(window, { time_us: 200 }).orders[key('A', 'a')]).toMatchObject({ confirmed: true, price: 99,
      attributes: { later: false }, remaining_volume: 10 })
    expect(projectReplay(window, { time_us: 400 }).orders[key('A', 'a')]).toMatchObject({ terminal: true, confirmed: false,
      filled_volume: 10, remaining_volume: 0, price: 101, attributes: { reason: '迟到补充' } })
  })

  test('扩展属性省略或null保留，显式对象替换而field_quality按字段合并', () => {
    const messages = [message(1, '/order', order('a', { attributes: { original: true }, native_identity: { original: 'A' }, field_quality: { quantity: 'invalid' } })),
      message(2, '/order', { symbol: 'A', order_id: 'a', sync_generation: 1, attributes: null, field_quality: { price: 'known' } }),
      message(3, '/order', { symbol: 'A', order_id: 'a', sync_generation: 1, attributes: {}, native_identity: { replacement: 'B' } }),
      message(4, '/trade', fill('T', 4, { order_id: 'a', attributes: { original: true }, native_identity: { original: 'A' } })),
      message(5, '/trade', fill('T', 4, { order_id: 'a', attributes: null })),
      message(6, '/trade', fill('T', 4, { order_id: 'a', attributes: {}, native_identity: { replacement: 'B' } }))]
    const window = windowOf(messages)
    expect(projectReplay(window, { time_us: 200 }).orders[key('A', 'a')]).toMatchObject({ attributes: { original: true }, native_identity: { original: 'A' }, volume: null })
    expect(projectReplay(window, { time_us: 300 }).orders[key('A', 'a')].attributes).toEqual({})
    expect(projectReplay(window, { time_us: 300 }).orders[key('A', 'a')].native_identity).toEqual({ replacement: 'B' })
    expect(projectReplay(window, { time_us: 500 }).ownTrades[0]).toMatchObject({ attributes: { original: true }, native_identity: { original: 'A' } })
    expect(projectReplay(window, { time_us: 600 }).ownTrades[0].attributes).toEqual({})
    expect(projectReplay(window, { time_us: 600 }).ownTrades[0].native_identity).toEqual({ replacement: 'B' })
  })

  test('placement结果只补归属，不造受理；cancel/error不能错误绑定，未来成交复用证明', () => {
    const messages = [message(1, '/execution', { execution_id: 'E', phase: 'start', symbols: ['A'] }),
      message(2, '/order', order('a', { execution_id: null, confirmed_book: false })),
      message(3, '/trade', fill('T', 4, { execution_id: null, order_id: 'a' })),
      message(4, '/command', { command_id: 'bad', operation: 'cancel_order', phase: 'result', execution_id: 'E', symbol: 'A', order_id: 'a' }),
      message(5, '/command', { command_id: 'placement', operation: 'place_order', phase: 'result', execution_id: 'E', symbol: 'A', order_id: 'a' }),
      message(6, '/trade', fill('U', 2, { execution_id: null, order_id: 'a' })),
      message(7, '/command', { command_id: null, operation: 'place_order', phase: 'error', symbol: 'A' }),
      message(8, '/order', { symbol: 'A', order_id: null, sync_generation: 1, confirmed_book: true })]
    const window = windowOf(messages)
    expect(projectReplay(window, { time_us: 400 }).ownTrades[0].execution_id).toBeNull()
    const bound = projectReplay(window, { time_us: 600 })
    expect(bound.orders[key('A', 'a')]).toMatchObject({ execution_id: 'E', confirmed: false, filled_volume: 6, remaining_volume: 4 })
    expect(bound.executionTotals.E.A).toBe(6)
    expect(bound.executionSymbolComplete.E.A).toBe(true)
    const last = projectReplay(window, { time_us: 800 })
    expect(last.commands.at(-1)?.command_id).toBe('observation:7')
    expect(last.warnings.some(text => text.includes('命令身份'))).toBe(true)
    expect(last.orders[key('A', 'unknown-observation:8')].confirmed).toBe(false)
  })

  test('wrong recording query_basis不清集合，连接新代即使connected也先使旧确认失效', () => {
    const window = windowOf([message(1, '/order', order('a')),
      message(2, '/order_set', { scope: ['A'], scope_complete: true, synchronized: true, sync_generation: 1,
        query_basis: { start_seq: 1, request_revision: 1, recording_id: 'rec-B' }, orders: [] }),
      message(3, '/connection', { connected: true, scope: { kind: 'account', symbols: [] }, sync_generation: 2 })])
    expect(projectReplay(window, { time_us: 200 }).orders[key('A', 'a')].confirmed).toBe(true)
    expect(projectReplay(window, { time_us: 300 }).orders[key('A', 'a')].confirmed).toBe(false)
  })

  test('已保存placement证明不因returned丢失，成交枚举大小写不构成新事实', () => {
    const messages = [message(1, '/execution', { execution_id: 'E', phase: 'start', symbols: ['A'] }),
      message(2, '/command', { command_id: 'p', operation: 'place_order', phase: 'result', execution_id: 'E', symbol: 'A', order_id: 'a' }),
      message(3, '/command', { command_id: 'p', phase: 'returned' }),
      message(4, '/order', order('a', { execution_id: null })),
      message(5, '/trade', fill('T', 4, { order_id: 'a', execution_id: null, offset: 'OPEN' })),
      message(6, '/trade', fill('T', 4, { order_id: 'a', execution_id: null, direction: 'buy', position_side: 'net', offset: 'open' })),
      message(7, '/order_set', { scope: ['A'], scope_complete: true, synchronized: true, sync_generation: 1,
        query_basis: { start_seq: 5, request_revision: 1 }, orders: [] })]
    const result = projectReplay(windowOf(messages), { time_us: 700 })
    expect(result.orders[key('A', 'a')].execution_id).toBe('E')
    expect(result.orders[key('A', 'a')].confirmed).toBe(false)
    expect(result.executionSymbolComplete.E.A).toBe(true)
    expect(result.executionTotals.E.A).toBe(4)
    expect(result.ownTrades[0].conflict).toBe(false)
  })

  test('同稳定身份冲突保留原字段且不再给出精确剩余或完成量', () => {
    const messages = [message(1, '/execution', { execution_id: 'E', phase: 'start', symbols: ['A'] }),
      message(2, '/order', order('a')),
      message(3, '/trade', fill('T', 4, { order_id: 'a', execution_id: 'E' })),
      message(4, '/trade', fill('T', 7, { order_id: 'a', execution_id: 'E', price: 100 }))]
    const result = projectReplay(windowOf(messages), { time_us: 400 })
    expect(result.ownTrades[0]).toMatchObject({ quantity: 4, price: 99, conflict: true })
    expect(result.orders[key('A', 'a')].remaining_volume).toBeNull()
    expect(result.executionSymbolComplete.E.A).toBe(false)
  })

  test.each(['connected', 'ready', 'available', 'synchronized'])('同代次的%s失效也撤销盘口和订单确认，健康连接不自行恢复', flag => {
    const messages = [message(1, '/quote/A', { symbol: 'A', book_valid: true, available: true, valid_until_us: 1000,
      bids: [{ price: 98, quantity: 2 }], asks: [{ price: 99, quantity: 3 }] }),
    message(2, '/order', order('a')),
    message(3, '/connection', { scope: { kind: 'account', symbols: [] }, sync_generation: 1, status: 'down', [flag]: false }),
    message(4, '/connection', { scope: { kind: 'account', symbols: [] }, sync_generation: 1, connected: true })]
    const result = projectReplay(windowOf(messages), { time_us: 400 })
    expect(result.orders[key('A', 'a')].confirmed).toBe(false)
    expect(result.quotes.A.available).toBe(false)
  })

  test.each(['returned', 'result', 'native_request', 'native_return', 'native_returned'])('撤单%s只是意图，完整重放和新旧检查点恢复都保持待确认', phase => {
    const request = { command_id: 'cancel-a', operation: 'cancel_order', symbol: 'A', order_id: 'a' }
    const latest = { ...request, phase, success: phase === 'result' ? true : null }
    const messages = [message(1, '/order', order('a')), message(2, '/command', { ...request, phase: 'request' }),
      message(3, '/command', latest)]
    const full = projectReplay(windowOf(messages), { time_us: 300 })
    for (const cached of [undefined, true]) {
      const carried = projectReplay(windowOf([], { state_through_seq: 3, time_us: 300,
        orders: { [key('A', 'a')]: { ...order('a'), cancel_pending: cached, observed_at_us: 100, observed_seq: 1 } },
        commands: { 'cancel-a': { ...latest, observed_at_us: 300, observed_seq: 3 } },
      }), { time_us: 300 })
      expect(carried.orders[key('A', 'a')].cancel_pending).toBe(full.orders[key('A', 'a')].cancel_pending)
      expect(carried.orders[key('A', 'a')]).toMatchObject({ cancel_pending: true, confirmed: true, terminal: false, status: 'working' })
      expect(carried.orderHistory).toHaveLength(1)
    }
  })

  test.each([{ phase: 'error' }, { phase: 'native_error' }, { phase: 'result', success: false },
    { phase: 'native_return', accepted: false }])('失败撤单按命令身份解除意图，原委托保持有效：%o', failure => {
    const request = { command_id: 'cancel-a', operation: 'cancel_order', symbol: 'A', order_id: 'a', phase: 'request' }
    const messages = [message(1, '/order', order('a')), message(2, '/command', request),
      message(3, '/command', { command_id: 'cancel-a', ...failure })]
    const window = windowOf(messages)
    expect(projectReplay(window, { time_us: 200 }).orders[key('A', 'a')].cancel_pending).toBe(true)
    const result = projectReplay(window, { time_us: 300 })
    expect(result.orders[key('A', 'a')]).toMatchObject({ cancel_pending: false, confirmed: true, terminal: false, status: 'working' })
    const restored = projectReplay(windowOf([], { state_through_seq: 3, time_us: 300,
      orders: { [key('A', 'a')]: { ...order('a'), cancel_pending: true } },
      commands: { 'cancel-a': { ...request, ...failure, observed_seq: 3, observed_at_us: 300 } },
    }), { time_us: 300 })
    expect(restored.orders[key('A', 'a')].cancel_pending).toBe(false)
  })

  test('多次撤单各自收敛，缺身份错误不清其他意图，订单终态清除全部待撤', () => {
    const request = { operation: 'cancel_order', symbol: 'A', order_id: 'a', phase: 'request' }
    const messages = [message(1, '/order', order('a')),
      message(2, '/command', { ...request, command_id: 'c1' }), message(3, '/command', { ...request, command_id: 'c2' }),
      message(4, '/command', { command_id: 'c1', phase: 'error' }),
      message(5, '/command', { ...request, command_id: null, phase: 'error' }),
      message(6, '/command', { command_id: 'c2', phase: 'result', success: false }),
      message(7, '/command', { ...request, command_id: 'c3' }),
      message(8, '/order', order('a', { status: 'filled', filled_quantity: 10, remaining_quantity: 0 })),
      message(9, '/order', order('a'))]
    const window = windowOf(messages)
    expect(projectReplay(window, { time_us: 500 }).orders[key('A', 'a')].cancel_pending).toBe(true)
    expect(projectReplay(window, { time_us: 600 }).orders[key('A', 'a')].cancel_pending).toBe(false)
    expect(projectReplay(window, { time_us: 700 }).orders[key('A', 'a')].cancel_pending).toBe(true)
    expect(projectReplay(window, { time_us: 900 }).orders[key('A', 'a')]).toMatchObject({ terminal: true, cancel_pending: false, confirmed: false })
  })

  test('撤单意图先于订单观察可恢复，柜台待撤证据不被本地重试失败清掉', () => {
    const request = { operation: 'cancel_order', command_id: 'c1', symbol: 'A', order_id: 'a', phase: 'request' }
    const messages = [message(1, '/command', request), message(2, '/order', order('a')),
      message(3, '/order', order('a', { status: 'pending_cancel' })),
      message(4, '/command', { command_id: 'c1', phase: 'error' })]
    expect(projectReplay(windowOf(messages), { time_us: 200 }).orders[key('A', 'a')].cancel_pending).toBe(true)
    expect(projectReplay(windowOf(messages), { time_us: 400 }).orders[key('A', 'a')].cancel_pending).toBe(true)
  })

  test('恢复检查点先载入完整命令事实，不把部分恢复的单一归属当成证明', () => {
    const state = { state_through_seq: 4, time_us: 400,
      orders: { [key('A', 'a')]: order('a') },
      commands: { c1: { command_id: 'c1', operation: 'place_order', phase: 'result', symbol: 'A', order_id: 'a', execution_id: 'E' },
        c2: { command_id: 'c2', operation: 'place_order', phase: 'result', symbol: 'A', order_id: 'a', execution_id: 'F' } },
      trade_accounting: { own_seen_keys: ['T'], market_seen_keys: [], own_complete: false,
        own_records: { T: { data: fill('T', 1, { order_id: 'a', execution_id: null }), first_time_us: 100, first_capture_seq: 1 } }, market_records: {} },
    }
    const result = projectReplay(windowOf([], state), { time_us: 400 })
    expect(result.orders[key('A', 'a')].execution_id).toBeNull()
    expect(result.ownTrades[0].execution_id).toBeNull()
  })

  test.each([true, false])('完整集合缺席在全播和原生检查点一致，原确认=%s，新实时确认仍可恢复', confirmed => {
    const source = order('a', { confirmed_book: confirmed })
    const query = message(2, '/order_set', { scope: { kind: 'account', symbols: [] }, scope_complete: true,
      synchronized: true, sync_generation: 1, query_basis: { start_seq: 1, request_revision: 1 }, orders: [] })
    const messages = [message(1, '/order', source), query]
    const state = { state_through_seq: 2, time_us: 200,
      orders: { [key('A', 'a')]: { ...source, confirmed_book: false, confirmation_state: 'absent', observed_seq: 1, observed_at_us: 100 } },
      sync_generation: { A: 1 }, last_set_revision: { A: 1 }, order_change_seq: { [key('A', 'a')]: 1 } }
    for (const window of [windowOf(messages), windowOf([], state)]) {
      const before = structuredClone(window)
      const value = projectReplay(window, { time_us: 200 }).orders[key('A', 'a')]
      expect(value).toMatchObject({ status: 'unknown', observed_status: 'working', confirmed: false, terminal: false })
      expect(window).toEqual(before)
      const next = projectReplay({ ...window, messages: [...window.messages, message(3, '/order', { ...source, confirmed_book: true })] }, { time_us: 300 })
      expect(next.orders[key('A', 'a')]).toMatchObject({ status: 'working', observed_status: 'working', confirmed: true })
    }
  })

  test.each(['account', 'other-symbol'])('缺席后%s失联及另一订单进入新代次，不恢复旧单状态', scope => {
    const source = order('a'), next = order('b', { sync_generation: 2 })
    const messages = [message(1, '/order', source), message(2, '/order_set', {
      scope: { kind: 'account', symbols: [] }, scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { start_seq: 1, request_revision: 1 }, orders: [],
    }), message(3, '/connection', { scope: scope === 'account' ? 'account' : { symbols: ['B'] }, connected: false, sync_generation: 2 }),
    message(4, '/order', next)]
    const checkpoint = { state_through_seq: 4, time_us: 400, sync_generation: { A: 2 }, orders: {
      [key('A', 'a')]: { ...source, confirmed_book: false, confirmation_state: 'absent', observed_seq: 1, observed_at_us: 100 },
      [key('A', 'b')]: { ...next, observed_seq: 4, observed_at_us: 400 },
    } }
    for (const window of [windowOf(messages), windowOf([], checkpoint)]) {
      const value = projectReplay(window, { time_us: 400 })
      expect(value.orders[key('A', 'a')]).toMatchObject({ status: 'unknown', observed_status: 'working', confirmed: false, terminal: false })
      expect(value.orders[key('A', 'b')]).toMatchObject({ status: 'working', confirmed: true })
      const recovered = projectReplay({ ...window, messages: [...window.messages, message(5, '/order', { ...source, sync_generation: 2 })] }, { time_us: 500 })
      expect(recovered.orders[key('A', 'a')]).toMatchObject({ status: 'working', confirmed: true })
    }
  })

  test('缺席只改变显示，不抹掉原始柜台待撤证据', () => {
    const source = order('a', { status: 'pending_cancel' })
    const cancel = { command_id: 'c', operation: 'cancel_order', phase: 'error', symbol: 'A', order_id: 'a' }
    const query = { scope: { symbols: ['A'] }, scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { start_seq: 2, request_revision: 1 }, orders: [] }
    const full = windowOf([message(1, '/order', source), message(2, '/command', cancel), message(3, '/order_set', query)])
    const restored = windowOf([], { state_through_seq: 3, time_us: 300,
      orders: { [key('A', 'a')]: { ...source, confirmed_book: false, confirmation_state: 'absent' } }, commands: { c: cancel } })
    for (const window of [full, restored]) expect(projectReplay(window, { time_us: 300 }).orders[key('A', 'a')])
      .toMatchObject({ status: 'unknown', observed_status: 'pending_cancel', confirmed: false, cancel_pending: true })
  })

  test('重复完整空集合只记录第一次缺席变化，不让相同未知状态膨胀历史', () => {
    const query = (capture_seq: number) => message(capture_seq, '/order_set', { scope: { symbols: ['A'] },
      scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { start_seq: capture_seq - 1, request_revision: capture_seq - 2 }, orders: [] })
    const messages = [message(1, '/order', order('a')), message(2, '/order', order('b', { confirmed_book: false })), query(3), query(4)]
    const result = projectReplay(windowOf(messages), { time_us: 400 })
    expect(result.orderHistory).toHaveLength(4)
    expect(result.orderHistory.slice(-2).map(value => value.status)).toEqual(['unknown', 'unknown'])
  })

  test('恢复全部命令后只统一计算一次关联，避免随历史命令重复扫描所有订单', () => {
    const count = 64, orders: ReplayData = {}, commands: ReplayData = {}
    let qualityReads = 0
    for (let index = 0; index < count; index++) {
      const id = String(index)
      orders[key('A', id)] = order(id, { status: 'filled', filled_quantity: 10, remaining_quantity: 0, confirmed_book: false })
      commands[id] = { command_id: id, operation: 'place_order', phase: 'result', symbol: 'A', order_id: id,
        execution_id: 'E', get field_quality() { qualityReads++; return {} } }
    }
    const result = projectReplay(windowOf([], { orders, commands }), { time_us: 0 })
    expect(result.commands).toHaveLength(count)
    expect(Object.values(result.orders).every(value => value.execution_id === 'E')).toBe(true)
    expect(qualityReads).toBeLessThan(count * 20)
  })

  test('恢复待关联观察不改写输入基线，多次定位仍保留各自前缀', () => {
    const window = windowOf([], { state_through_seq: 3, time_us: 300,
      commands: { c: { command_id: 'c', operation: 'place_order', phase: 'result', execution_id: 'E', symbol: 'A', order_id: 'a' } },
      executions: { E: { execution_id: 'E', start_observed: true, symbols: ['A'] } },
      trade_accounting: { own_seen_keys: [], market_seen_keys: [], own_records: {}, market_records: {}, own_complete: false,
        pending: { 'own:observation:1': { market: false, observation: { symbol: 'A', order_id: 'a', quantity: 1,
          identity_quality: 'weak', trade_key: null, observed_at_us: 100, observed_seq: 1 } } } } })
    const before = structuredClone(window)
    for (const time_us of [300, 400, 300]) {
      expect(projectReplay(window, { time_us }).executionSymbolComplete.E.A).toBe(false)
      expect(window).toEqual(before)
    }
  })

  test('归属展示保留订单来源，全播与检查点一致且不把无效归属当已知', () => {
    const source = order('a', { execution_id: 'E', origin: 'manual' })
    const invalid = order('b', { execution_id: 'E', origin: 'external', field_quality: { execution_id: 'invalid', origin: 'missing' } })
    const full = windowOf([message(1, '/order', source), message(2, '/order', invalid)])
    const carried = windowOf([], { state_through_seq: 2, time_us: 200,
      orders: { [key('A', 'a')]: source, [key('A', 'b')]: invalid } })
    for (const window of [full, carried]) {
      const view = projectReplay(window, { time_us: 200 })
      expect(view.orders[key('A', 'a')]).toMatchObject({ execution_id: 'E', origin: 'manual' })
      expect(view.orders[key('A', 'b')]).toMatchObject({ execution_id: null, origin: null })
    }
  })

  test('归属展示不采用成交中质量无效的执行身份与来源', () => {
    const value = fill('T', 1, { order_id: 'a', origin: 'manual', field_quality: { execution_id: 'missing', origin: 'invalid' } })
    const view = projectReplay(windowOf([message(1, '/trade', value)]), { time_us: 100 })
    expect(view.ownTrades[0]).toMatchObject({ execution_id: null, origin: null })
  })

  test('空委托范围的查询水位仍约束后到的账户集合', () => {
    const messages = [message(1, '/execution', { execution_id: 'E', phase: 'start', symbols: ['X'] }),
      message(2, '/order_set', { scope: { kind: 'symbols', symbols: ['X'] }, synchronized: true, scope_complete: true,
        sync_generation: 1, query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 2 }, orders: [] }),
      message(3, '/order_set', { scope: { kind: 'account', symbols: [] }, synchronized: true, scope_complete: true,
        sync_generation: 1, query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 1 }, orders: [] }),
      message(4, '/order', order('Y1', { symbol: 'Y', price: 100,
        query_basis: { recording_id: 'rec-A', start_seq: 1, request_revision: 3 } }))]
    expect(projectReplay(windowOf(messages), { time_us: 400 }).orders[key('Y', 'Y1')]).toMatchObject({ confirmed: true, price: 100 })
  })

  test.each([
    { sync_generation: { X: 1 } },
    { connections: { X: { sync_generation: null } } },
    { position_scopes: { X: { scope_complete: true } } },
    { position_basis: { '["X","LONG"]': { status: 'unverified' } } },
  ])('账户断线覆盖已有但无可见行的范围 %j', state => {
    const messages = [message(2, '/connection', { scope: { kind: 'account', symbols: [] }, connected: false, sync_generation: 2 }),
      message(3, '/order', order('O', { symbol: 'X', sync_generation: 1, confirmed_book: false })),
      message(4, '/order', order('O', { symbol: 'X', sync_generation: 2 }))]
    const window = windowOf(messages, { state_through_seq: 1, time_us: 100, ...state })
    expect(projectReplay(window, { time_us: 300 }).orders[key('X', 'O')]).toBeUndefined()
    expect(projectReplay(window, { time_us: 400 }).orders[key('X', 'O')].confirmed).toBe(true)
  })

  test.each(['command', 'trade'])('纯成交归属补充不阻止在途空集合确认缺席 %s', source => {
    const observations = [message(1, '/execution', { execution_id: 'E', phase: 'start', symbols: ['A'] }),
      message(2, '/order', order('O')),
      message(3, '/trade', fill('T', 1, { execution_id: null, origin: 'unknown', order_id: 'O' }))]
    const metadata = source === 'command'
      ? message(4, '/command', { command_id: 'C', operation: 'place_order', phase: 'result', execution_id: 'E', symbol: 'A', order_id: 'O' })
      : message(4, '/trade', fill('T', 1, { execution_id: 'E', origin: 'execution', order_id: 'O' }))
    const complete = message(5, '/order_set', { scope: ['A'], scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { recording_id: 'rec-A', start_seq: 3, request_revision: 1 }, orders: [] })
    const view = projectReplay(windowOf([...observations, metadata, complete]), { time_us: 500 })
    expect(view.orders[key('A', 'O')]).toMatchObject({ status: 'unknown', confirmed: false })
    if (source === 'command') expect(view.orders[key('A', 'O')]).toMatchObject({ execution_id: 'E', origin: 'execution' })
    expect(view.ownTrades[0].execution_id).toBe('E')
    expect(view.executionTotals.E.A).toBe(1)
  })

  test.each(['symbol', 'price', 'quantity', 'direction', 'position_side', 'offset', 'order_id'].flatMap(field =>
    ['missing', 'invalid'].flatMap(quality => ['quality-only-known', 'omitted-quality', 'known-rejects-invalid'].map(mode => ({ field, quality, mode }))))
  )('成交质量合并保持原生水位：$field / $quality / $mode', ({ field, quality, mode }) => {
    const original = fill('T', 1, { order_id: 'a', offset: 'OPEN', field_quality: { [field]: mode === 'known-rejects-invalid' ? 'known' : quality } })
    const completion: ReplayData = { trade_key: 'T', identity_quality: 'stable', execution_id: 'exec-1',
      ...(mode === 'quality-only-known' ? { field_quality: { [field]: 'known' } }
        : { [field]: original[field], ...(mode === 'known-rejects-invalid' ? { field_quality: { [field]: 'invalid' } } : {}) }) }
    const query = message(4, '/order_set', { scope: { symbols: ['A'] }, scope_complete: true, synchronized: true, sync_generation: 1,
      query_basis: { start_seq: 2, request_revision: 1 }, orders: [] })
    const prefix = [message(1, '/order', order('a')), message(2, '/trade', original)]
    const suffix = [message(3, '/trade', completion), query]
    const mid = { state_through_seq: 2, time_us: 200, orders: { [key('A', 'a')]: order('a') },
      sync_generation: { A: 1 }, order_change_seq: { [key('A', 'a')]: 2 }, trade_accounting: {
        own_complete: true, own_records: { T: { data: original, first_time_us: 200, first_capture_seq: 2, last_fact_seq: 2 } },
      } }
    for (const window of [windowOf([...prefix, ...suffix]), windowOf(suffix, mid)]) {
      const value = projectReplay(window, { time_us: 400 }), transaction = value.ownTrades.find(row => row.key === 'T')!
      expect(value.orders[key('A', 'a')].confirmed).toBe(mode === 'quality-only-known')
      expect(transaction.quantity).toBe(field === 'quantity' && mode === 'omitted-quality' ? null : 1)
      expect(transaction.price).toBe(field === 'price' && mode === 'omitted-quality' ? null : 99)
      expect(transaction.conflict).toBe(false)
    }
  })

  test('稳定成交尚无品种时保留原始事实，后续只补品种仍可全播或从检查点恢复', () => {
    const original = fill('T', 1, { symbol: null, order_id: 'a' })
    const first = message(1, '/trade', original)
    const completion = message(2, '/trade', { symbol: 'A', trade_key: 'T', identity_quality: 'stable' })
    const checkpoint = { state_through_seq: 1, time_us: 100, trade_accounting: { own_complete: false, own_seen_keys: ['T'],
      own_records: { T: { data: original, first_time_us: 100, first_capture_seq: 1, last_fact_seq: 1 } }, pending: {},
    } }
    expect(projectReplay(windowOf([first]), { time_us: 100 }).accountingComplete).toBe(false)
    for (const window of [windowOf([first, completion]), windowOf([completion], checkpoint)]) {
      const value = projectReplay(window, { time_us: 200 })
      expect(value.ownTrades).toHaveLength(1)
      expect(value.ownTrades[0]).toMatchObject({ symbol: 'A', quantity: 1, price: 99, order_id: 'a' })
      expect(value.accountingComplete).toBe(false)
      expect(value.executionTotals['exec-1'].A).toBe(1)
    }
  })

  test.each(['new', 'identity', 'quantity', 'price'])('真实成交或身份变化仍保护查询期间的订单 %s', change => {
    const first = fill('T', 1, { quantity: change === 'quantity' ? null : 1,
      order_id: change === 'identity' ? null : 'O', price: change === 'price' ? null : 99 })
    const observations = [message(1, '/order', order('O')), message(2, '/trade', first),
      message(3, '/trade', fill(change === 'new' ? 'U' : 'T', 1, { order_id: 'O' })),
      message(4, '/order_set', { scope: ['A'], scope_complete: true, synchronized: true, sync_generation: 1,
        query_basis: { recording_id: 'rec-A', start_seq: 2, request_revision: 1 }, orders: [] })]
    expect(projectReplay(windowOf(observations), { time_us: 400 }).orders[key('A', 'O')].confirmed).toBe(true)
  })

  test.each(['account', 'none', 'symbol'])('筛选后的集合保留原生已接纳的范围语义 %s', accepted => {
    const complete = message(1, '/order_set', { scope: { kind: accepted === 'account' ? 'account' : 'symbols',
      symbols: accepted === 'symbol' ? ['B'] : [] }, synchronized: true, scope_complete: true, sync_generation: 1,
      query_basis: { recording_id: 'rec-A', start_seq: 0, request_revision: 1 },
      orders: accepted === 'symbol' ? [order('OB', { symbol: 'B', price: 101 })] : [] })
    const stale = message(2, '/order', order('OB', { symbol: 'B', price: 100,
      query_basis: { recording_id: 'rec-A', start_seq: 0, request_revision: 2 } }))
    const value = projectReplay(windowOf([complete, stale]), { time_us: 200 }).orders[key('B', 'OB')]
    if (accepted === 'account') expect(value).toBeUndefined()
    else expect(value).toMatchObject({ confirmed: true, price: accepted === 'symbol' ? 101 : 100 })
  })

  test.each(['symbols', 'account'])('完整账户新水位阻止旧完整集合复活此前未知的B %s', kind => {
    const account = message(2, '/order_set', { scope: { kind: 'account', symbols: [] }, synchronized: true,
      scope_complete: true, sync_generation: 1, query_basis: { start_seq: 1, request_revision: 2 }, orders: [] })
    const response = (generation: number, revision: number) => message(3, '/order_set', { scope: { kind, symbols: kind === 'symbols' ? ['B'] : [] },
      synchronized: true, scope_complete: true, sync_generation: generation,
      query_basis: { start_seq: 1, request_revision: revision }, orders: [order('OB', { symbol: 'B', sync_generation: generation })] })
    const before = [message(1, '/execution', { execution_id: 'E', phase: 'start' }), account]
    expect(projectReplay(windowOf([...before, response(1, 1)]), { time_us: 300 }).orders[key('B', 'OB')]).toBeUndefined()
    for (const [generation, revision] of [[1, 3], [2, 1]]) {
      expect(projectReplay(windowOf([...before, response(generation, revision)]), { time_us: 300 }).orders[key('B', 'OB')].confirmed).toBe(true)
    }
  })

  test.each(['partial', 'unproven'])('账户非完整或无证明的集合不能形成完整集合屏障 %s', kind => {
    const account = message(2, '/order_set', { scope: { kind: 'account', symbols: [] }, synchronized: true,
      scope_complete: kind !== 'partial', sync_generation: 1,
      query_basis: kind === 'unproven' ? null : { start_seq: 1, request_revision: 2 }, orders: [] })
    const narrow = message(3, '/order_set', { scope: ['B'], synchronized: true, scope_complete: true, sync_generation: 1,
      query_basis: { start_seq: 1, request_revision: 1 }, orders: [order('OB', { symbol: 'B' })] })
    expect(projectReplay(windowOf([message(1, '/execution', { execution_id: 'E', phase: 'start' }), account, narrow]),
      { time_us: 300 }).orders[key('B', 'OB')].confirmed).toBe(true)
  })
})

for (const scenario of quoteQuality) test(`盘口结构与本地有效期连播/恢复一致：${scenario.name}`, () => {
  const observed = 1_000_000
  const data = { symbol: 'X', bids: scenario.bids.map(([price, quantity]) => ({ price, quantity })),
    asks: scenario.asks.map(([price, quantity]) => ({ price, quantity })), book_valid: scenario.book_valid,
    received_time_us: observed, source_time_us: scenario.source_offset_us == null ? null : observed + scenario.source_offset_us,
    valid_until_us: scenario.validity_us == null ? null : observed + scenario.validity_us }
  const cursor = { time_us: observed + scenario.cursor_offset_us }
  const continuous = projectReplay(windowOf([message(1, '/quote/X', data, observed)]), cursor)
  const recovered = projectReplay(windowOf([], { state_through_seq: 1, time_us: observed,
    quotes: { X: { ...data, observed_at_us: observed, observed_seq: 1 } } }), cursor)
  expect(continuous.quotes.X).toEqual(recovered.quotes.X)
  expect(continuous.quotes.X.book_valid).toBe(scenario.book_valid)
  expect(continuous.quotes.X.available).toBe(scenario.available)
  expect(continuous.quotes.X.time_us).toBe(observed)
})
