import { expect, test } from 'bun:test'
import { costExecutions, costTrades, durationText, dailyCosts, executionsOnDay, loadPerformanceActivity, shanghaiDay, shanghaiLabel, shanghaiTime, summarizeCosts } from './costs'
import { loadJournal } from '@/features/account/executionJournal'
import type { AccountActivity } from '@/lib/api/accounts'
import type { ExecuteRecord, ExecutionResultFacts } from '@/types/api'

function record(trades: Record<string, unknown>[], result: Record<string, unknown> = {}): ExecuteRecord & { result: ExecutionResultFacts } {
  return { account_id: 1, id: 1, execution_id: 'test', created_at: '2026-01-01T09:00:00', is_success: 1, input: {},
    result: { status: 'SUCCEEDED', symbol_results: { RB: { sizing: { unit_multiplier: 10 }, first_tick: { bid_price: 99, ask_price: 101 },
      trades, orders: [{ order_id: 'b', direction: 'buy' }, { order_id: 's', direction: 'sell' }], ...result } } } }
}
const trade = (price: number, side = 'b', extra: Record<string, unknown> = {}) => ({ trade_price: price, trade_volume: 2, order_id: side, extra })
const listRow = (id: number, created_at: string): AccountActivity => ({
  kind: 'execution', occurred_at: created_at, record: {
    id, execution_id: `e${id}`, created_at, is_success: 1, symbol_results: {},
    summary: summarizeCosts([]), duration_sec: null, trade_count: 0,
  },
})

test('买卖成本按乘数计算，价格改善为负，混合方向逐笔相加', () => {
  const trades = costTrades(record([trade(101), trade(99), trade(99, 's'), trade(101, 's')]))
  expect(trades.map(t => t.cost)).toEqual([20, -20, 20, -20])
  expect(trades.map(t => t.lossBp)).toEqual([100, -100, 100, -100])
  expect(summarizeCosts(trades).cost).toBe(0)
  expect(summarizeCosts(trades).value).toBe(8000)
})

test('不从 trade_value 猜乘数，缺价格、方向、参考价时保留缺失', () => {
  expect(costTrades(record([{ ...trade(101), trade_value: 202 }], { sizing: null }))[0].cost).toBeNull()
  expect(costTrades(record([trade(101)], { first_tick: {} }))[0].cost).toBeNull()
  expect(costTrades(record([trade(101, 'unknown')]))[0].cost).toBeNull()
  const missing = costTrades(record([{ trade_volume: 2, order_id: 'b' }]))
  expect(missing).toHaveLength(1)
  expect(missing[0].cost).toBeNull()
  expect(summarizeCosts(missing).value).toBeNull()
})

test('最新价退化有来源，锁定盘口有效，倒挂盘口不作中间价', () => {
  expect(costTrades(record([trade(101)], { first_tick: { last_price: 100 } }))[0].referenceSource).toBe('last')
  expect(costTrades(record([trade(101)], { first_tick: { bid_price: 100, ask_price: 100 } }))[0].referenceSource).toBe('mid')
  expect(costTrades(record([trade(101)], { first_tick: { bid_price: 101, ask_price: 99 } }))[0].reference).toBeNull()
})

test('手续费缺失不补零，不同币种独立汇总，负收费返佣保留', () => {
  const summary = summarizeCosts(costTrades(record([trade(101, 'b', { commission: 0, commission_asset: 'CNY' }),
    trade(101, 'b', { commission: 2, commission_asset: 'USD' }), trade(99, 's', { commission: -0.1, commission_asset: 'CNY' }),
    trade(100), trade(100, 's', { commission: 3 })])))
  expect(summary.fees).toEqual({ CNY: -0.1, USD: 2 })
  expect(summary.feeCovered).toBe(3)
})

test('BP 按有效成交额加权，缺方向降低覆盖，缺乘数不能伪报覆盖率', () => {
  const trades = costTrades(record([trade(101), { ...trade(99), trade_volume: 10 }, trade(100, 'unknown')]))
  const summary = summarizeCosts(trades)
  expect(summary.lossBp).toBeCloseTo((2020 * 100 - 9900 * 100) / 11920)
  expect(summary.coverage).toBeCloseTo(11920 / 13920)
  expect(summary.covered).toBe(2)
  const missing = costTrades(record([trade(100)], { sizing: null }))
  expect(summarizeCosts([...trades, ...missing]).coverage).toBeNull()
})

test('失败、终止和部分成交都计成本；同目标有成交不为空跑', () => {
  for (const status of ['FAILED', 'TERMINATED', 'PARTIAL', 'NOOP']) {
    const r = record([trade(101)])
    r.result.task_status = status
    r.result.status = status
    expect(costExecutions([r])[0].summary.cost).toBe(20)
    expect(costExecutions([r])[0].noop).toBe(false)
  }
  const r = record([]); r.result.status = 'NOOP'
  expect(costExecutions([r])[0].noop).toBe(true)
  const filled = record([], { orders: [{ filled_volume: 2 }] }); filled.result.status = 'NOOP'
  expect(costExecutions([filled])[0].noop).toBe(false)
})

test('非对象历史结果没有可用成交，不从成功标志推断空跑', () => {
  for (const result of [null, '原始结果', 0, false, []]) {
    const saved: ExecuteRecord = { ...record([]), result }
    expect(costTrades(saved)).toEqual([])
    expect(costExecutions([saved])[0]).toMatchObject({ noop: false, summary: { value: null, cost: null } })
  }
})

test('上海跨年日分组，逐日和逐执行及品种汇总一致，无成交日不存在', () => {
  const r = record([{ ...trade(101), trade_time: '2025-12-31T16:01:00Z' }, { ...trade(99, 's'), trade_time: '2026-01-02T00:01:00+08:00' }])
  const executions = costExecutions([r])
  const days = dailyCosts(executions)
  expect([...days.keys()]).toEqual(['2026-01-01', '2026-01-02'])
  expect([...days.values()].reduce((sum, s) => sum + s.cost!, 0)).toBe(executions[0].summary.cost!)
  expect(summarizeCosts(executions.flatMap(e => e.trades).filter(t => t.symbol === 'RB')).cost).toBe(executions[0].summary.cost)
  expect(days.has('2026-01-03')).toBe(false)
  expect(shanghaiDay('2025-12-31T16:00:00Z')).toBe('2026-01-01')
  expect(shanghaiTime('2026-01-01T00:00:00')).toBe(shanghaiTime('2025-12-31T16:00:00Z'))
})

test('上海时间标签兼容历史空白，无法解析或超出范围时留空', () => {
  for (const value of ['2026-01-02T00:30:00', '2026-01-01T16:30:00.123456Z', ' 2026-01-02T00:30:00+08:00 ']) {
    expect(shanghaiLabel(value)).toBe('2026-01-02 00:30:00')
  }
  for (const value of ['', '  ', 'invalidTtime', '+275760-09-13T00:00:00.000Z']) {
    expect(shanghaiLabel(value)).toBe('')
  }
})

test('上海时间完整分页超过 500 条，边界包含基准与截止毫秒', async () => {
  const rows = Array.from({ length: 601 }, (_, i) => listRow(i, `2026-01-01T${i === 600 ? '08' : '09'}:00:00`))
  const calls: number[] = []
  const loaded = await loadJournal(1, { start: shanghaiTime('2026-01-01T09:00:00'), end: shanghaiTime('2026-01-01T09:00:00') + 1 }, new AbortController().signal,
    async skip => { calls.push(skip); return { count: rows.length, data: rows.slice(skip, skip + 500) } }, shanghaiTime)
  expect(loaded).toHaveLength(600)
  expect(calls).toEqual([0, 500])
})

test('同数量原地变更与复读失败均不发布，稳定数据完整返回', async () => {
  const window = { start: shanghaiTime('2026-01-01T00:00:00'), end: shanghaiTime('2026-01-02T00:00:00') }
  let calls = 0
  await expect(loadPerformanceActivity(1, window, new AbortController().signal, async () => {
    calls++
    return { count: 1, data: [listRow(calls, '2026-01-01T09:00:00')] }
  })).rejects.toThrow('发生变化')
  calls = 0
  await expect(loadPerformanceActivity(1, window, new AbortController().signal, async () => {
    if (++calls === 2) throw new Error('network error')
    return { count: 1, data: [listRow(1, '2026-01-01T09:00:00')] }
  })).rejects.toThrow('network error')
  const rows = [listRow(1, '2026-01-01T09:00:00')]
  expect(await loadPerformanceActivity(1, window, new AbortController().signal, async () => ({ count: 1, data: rows }))).toEqual(rows)
})

test('选日只汇总当天成交，跨日执行不会重复计成本，清除恢复区间', () => {
  const executions = costExecutions([record([{ ...trade(101), trade_time: '2026-01-01T23:59:00' }, { ...trade(99, 's'), trade_time: '2026-01-02T00:01:00' }])])
  expect(executionsOnDay(executions, '2026-01-01')[0].summary.cost).toBe(20)
  expect(executionsOnDay(executions, '2026-01-02')[0].summary.cost).toBe(20)
  expect(executionsOnDay(executions, '2026-01-03')).toEqual([])
  expect(executionsOnDay(executions, null)[0].summary.cost).toBe(40)
})


test('执行耗时保留零值、拒绝缺失与非法值并正确进位', () => {
  expect(durationText(0)).toBe('0.0s')
  expect(durationText(41.2)).toBe('41.2s')
  expect(durationText(65.5)).toBe('1m6s')
  expect(durationText(119.9)).toBe('2m0s')
  for (const value of [null, undefined, -1, NaN, Infinity]) expect(durationText(value)).toBe('—')
})
