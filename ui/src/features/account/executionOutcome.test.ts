import { expect, test } from 'bun:test'

import { executionRecordSummary, executionRecordView } from './executionOutcome'
import { withCurrency } from '@/lib/format'
import { buildExecutionDetail, executionHeadline } from './executionDetail'
import type { ExecutionArtifact } from '@/types/api'

test('摘要只统计已有品种和成交，缺失证据不补零个品种或零笔成交', () => {
  expect(executionRecordSummary({})).toBe('调仓 · 成交信息未获取')
  expect(executionRecordSummary({ execution_kind: 'clear_positions', symbol_results: { A: { trades: [] }, B: {} } })).toBe('清仓 · 涉及 2 个品种 · 未记录成交')
  const symbol_results = Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`symbol${i}`, { trades: i === 0 ? [{}, {}, {}, {}] : [] }]))
  expect(executionRecordSummary({ symbol_results })).toBe('调仓 · 涉及 30 个品种 · 4 笔成交')
})

test('只从记录已有字段生成受阻展示', () => {
  const view = executionRecordView({
    status: 'BLOCKED', error: '当前不在交易时段',
    symbol_results: { rb2610: { status: 'BLOCKED', trades: [] }, ag2612: { status: 'BLOCKED', trades: [] } },
  })
  expect(view).toMatchObject({ title: '未执行 · 2 个品种执行受阻', reason: '当前不在交易时段', affectedCount: 2, tradeCount: 0, tone: 'warn' })
})

test('BLOCKED 标题只认 reason_code，不从中文 error 推断', () => {
  const session = executionRecordView({
    status: 'BLOCKED', error: '非交易时段，6 个品种未执行', reason_code: 'COMMON.SESSION.CLOSED',
    symbol_results: { ag2612: { status: 'BLOCKED', error: '非交易时段' } },
  })
  expect(session).toMatchObject({ title: '休市跳过', reason: '非交易时段，6 个品种未执行' })
  const chineseOnly = executionRecordView({
    status: 'BLOCKED', error: '非交易时段',
    symbol_results: { ag2612: { status: 'BLOCKED', error: '非交易时段' } },
  })
  expect(chineseOnly.title).toBe('未执行 · 1 个品种执行受阻')
})

test('缺少状态不从错误文本推断结论', () => {
  expect(executionRecordView({ error: 'CLOSED' })).toMatchObject({ title: '执行状态未知', tone: 'neutral' })
})

test('30 个品种只展示数量，成交按原始数组统计', () => {
  const symbol_results = Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`SYMBOL${i}`, {
    status: i < 2 ? 'PARTIAL' : 'SUCCEEDED', trades: i < 4 ? [{}] : [],
  }]))
  expect(executionRecordView({ raw_result: { status: 'PARTIAL', symbol_results } })).toMatchObject({
    title: '执行未全部完成', symbolCount: 30, affectedCount: 2, tradeCount: 4, tone: 'warn',
  })
})

test.each([
  ['SUCCEEDED', '调仓完成', 'neutral'], ['NOOP', '无需调仓', 'neutral'],
  ['BLOCKED', '未执行', 'warn'], ['PARTIAL', '执行未全部完成', 'warn'],
  ['FAILED', '执行失败', 'warn'], ['invalid', '执行状态未知', 'neutral'],
])('详情和记录视图状态一致：%s', (status, title, tone) => {
  const raw = { status, error: '旧裸错误' }
  const artifacts: ExecutionArtifact[] = [{ id: 1, execution_id: 'test', created_at: '2026-09-15', artifact_type: 'execution_summary', content: raw }]
  expect(executionRecordView(raw)).toMatchObject({ title, tone, reason: '旧裸错误' })
  expect(executionHeadline(buildExecutionDetail([], artifacts)).text).toBe(title)
})

test('错误只按明确字段顺序回放，品种同因和不同因分别汇总', () => {
  expect(executionRecordView({ error: '第一', msg: '第二', memory: { message: '第三' } }).reason).toBe('第一')
  expect(executionRecordView({ msg: 'CLOSED', memory: { message: '第三' } }).reason).toBe('')
  expect(executionRecordView({ memory: { message: 'CLOSED', last_error: '不读' } }).reason).toBe('')
  const raw = { status: 'BLOCKED', symbol_results: { A: { status: 'BLOCKED', error: '休市' }, B: { status: 'BLOCKED', error: '休市' } } }
  expect(executionRecordView(raw).reason).toBe('休市，2 个品种执行受阻')
  raw.symbol_results.B.error = '行情不可用'
  expect(executionRecordView(raw).reason).toBe('2 个品种执行受阻，原因不同')
})

test('终止优先，旧完成摘要不能覆盖生命周期终止', () => {
  expect(executionRecordView({ task_status: 'TERMINATED', raw_result: { status: 'SUCCEEDED' } }).title).toBe('执行已终止')
})

test('类型化执行详情使用主记录结论和结果中的品种证据', () => {
  const record = {
    status: 'FAILED', task_status: 'FAILED', execution_kind: 'clear_positions', error: '执行收尾失败',
    result: { status: 'SUCCEEDED', error: '此前的结果', symbol_results: { A: { status: 'SUCCEEDED', trades: [{}] } } },
  }
  expect(executionRecordView(record)).toMatchObject({ title: '执行失败', reason: '执行收尾失败', symbolCount: 1, tradeCount: 1 })
  expect(executionRecordSummary(record)).toBe('清仓 · 涉及 1 个品种 · 1 笔成交')
  expect(executionRecordView({ task_status: 'TERMINATED', result: { status: 'SUCCEEDED' } }).title).toBe('执行已终止')
})

test('轻量记录使用明确计数，零值不被详情成交数量覆盖', () => {
  expect(executionRecordSummary({ status: 'SUCCEEDED', symbol_count: 30, trade_count: 4 })).toBe('调仓 · 涉及 30 个品种 · 4 笔成交')
  expect(executionRecordSummary({ status: 'NOOP', symbol_count: 0, trade_count: 0, result: { symbol_results: { A: { trades: [{}] } } } })).toBe('调仓 · 未记录成交')
  expect(executionRecordView({ is_success: 1, symbol_count: null, trade_count: null, result: {} })).toMatchObject({ state: 'UNKNOWN', tone: 'neutral' })
})

test('主记录明确的空状态和空原因不被结果内旧值覆盖', () => {
  expect(executionRecordView({ status: null, error: '', result: { status: 'SUCCEEDED', error: '旧错误' } }))
    .toMatchObject({ state: 'UNKNOWN', reason: '' })
  expect(executionRecordView({ status: 'BLOCKED', reason_code: null, result: { reason_code: 'COMMON.SESSION.CLOSED' } }).sessionScope).toBeNull()
})

test('使用持久化展示结论，原始状态和技术错误仍不改写', () => {
  const record = { status: null, error: 'Traceback: source', display_status: 'FAILED', display_error: '渠道连接失败', result: null }
  expect(executionRecordView(record)).toMatchObject({ state: 'FAILED', reason: '渠道连接失败', symbolCount: null, tradeCount: null })
  expect(record.error).toBe('Traceback: source')
  expect(executionRecordView({ display_status: null, status: 'NOOP', result: [] })).toHaveProperty('state', 'NOOP')
})

test('轻量活动未知成交数不写成零，显式零仍能辨认', () => {
  expect(executionRecordSummary({ status: 'SUCCEEDED', symbol_results: {}, trade_count: null })).toBe('调仓 · 成交信息未获取')
  expect(executionRecordSummary({ status: 'NOOP', symbol_results: {}, trade_count: 0 })).toBe('调仓 · 未记录成交')
  expect(executionRecordSummary({ result: { symbol_results: { A: { trades: [{}] } } } }, null)).toBe('调仓 · 涉及 1 个品种 · 成交信息未获取')
})

test('全休市是中性跳过，部分休市保留未完成和品种证据', () => {
  const closed = { status: 'BLOCKED', reason_code: 'COMMON.SESSION.CLOSED', error: '非交易时段', target_volume: 2, final_volume: null }
  expect(executionRecordView({ status: 'BLOCKED', task_status: 'SKIPPED', reason_code: 'COMMON.SESSION.CLOSED', symbol_results: { m2701: closed } }))
    .toMatchObject({ title: '休市跳过', tone: 'neutral', warning: false, sessionScope: 'all', symbols: ['m2701'] })
  for (const status of ['PARTIAL', 'BLOCKED']) {
    expect(executionRecordView({ status, reason_code: 'COMMON.SESSION.CLOSED', symbol_results: { m2701: closed, ag2612: { status: 'NOOP' } } }))
      .toMatchObject({ title: '部分品种休市 · 执行未全部完成', tone: 'warn', sessionScope: 'partial', symbols: ['m2701'] })
  }
  expect(closed.final_volume).toBeNull()
})

test('真实错误或订单证据不会被休市原因遮住', () => {
  expect(executionRecordView({ status: 'PARTIAL', reason_code: null, error: '最终持仓查询失败', symbol_results: { m2701: { status: 'BLOCKED', reason_code: 'COMMON.SESSION.CLOSED' }, ag2612: { status: 'NOOP' } } }))
    .toMatchObject({ title: '执行未全部完成', reason: '最终持仓查询失败', sessionScope: null, warning: true })
  expect(executionRecordView({ status: 'BLOCKED', reason_code: 'COMMON.SESSION.CLOSED', symbol_results: { m2701: { status: 'BLOCKED', orders: [{}] } } }).sessionScope).toBeNull()
  expect(executionRecordView({ status: 'FAILED', reason_code: 'COMMON.SESSION.CLOSED' }).title).toBe('执行失败')
})

test('详情逐只状态使用原始结果，不把受阻且缺仓位的品种显示为到位', () => {
  const content = { status: 'BLOCKED', symbol_results: { A: { status: 'BLOCKED', error: '休市', orders: [], trades: [] } } }
  const artifacts: ExecutionArtifact[] = [{ id: 1, execution_id: 'test', created_at: '2026-09-15', artifact_type: 'execution_summary', content }]
  const symbol = buildExecutionDetail([], artifacts).symbols[0]
  expect(symbol).toMatchObject({ status: 'BLOCKED', action: 'skipped', observedAfter: null, reason: '休市', broken: true })
})

test.each(['USD', 'EUR', 'JPY', 'CNY', ''])('币种原样保留：%s', currency => {
  expect(withCurrency('10', currency)).toBe(currency === 'CNY' ? '¥10' : currency ? `10 ${currency}` : '10')
})
