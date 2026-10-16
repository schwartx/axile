import { expect, test } from 'bun:test'
import { buildRecentActivity, recentRowText } from './recent'
import { summarizeCosts } from '@/features/history/costs'
import type { AccountActivity, ActivityExecutionRecord } from '@/lib/api/accounts'

const empty = summarizeCosts([])
function execution(id: number, state?: string, extra: Partial<ActivityExecutionRecord> = {}): AccountActivity {
  return { kind: 'execution', occurred_at: `2026-09-14T14:${id}:00`, record: {
    id, execution_id: `e${id}`, created_at: `2026-09-14T14:${id}:00`, is_success: 0,
    status: state, symbol_results: {}, summary: empty, duration_sec: null, trade_count: 0, ...extra,
  } }
}
const rows = (items: AccountActivity[], cap = 6, fetchLimit = 50) => buildRecentActivity(items, { cap, fetchLimit })

test('休市跳过与其他阻断分开折叠，部分休市保留未处理数量', () => {
  const result = rows([
    execution(24, 'BLOCKED', { task_status: 'SKIPPED', reason_code: 'COMMON.SESSION.CLOSED' }),
    execution(23, 'BLOCKED', { reason_code: 'CHANNEL.QUERY_FAILED' }),
    execution(22, 'PARTIAL', { reason_code: 'COMMON.SESSION.CLOSED', symbol_results: { m2701: { status: 'BLOCKED', reason_code: 'COMMON.SESSION.CLOSED' }, ag2612: { status: 'NOOP' } } }),
  ])
  expect(result.rows.map(recentRowText)).toEqual(['休市跳过', '未执行', '执行未全部完成 · 部分品种休市，1 个品种未处理'])
})

test('最新清仓成功保留在前，旧失败不覆盖最新结论', () => {
  const result = rows([execution(23, 'SUCCEEDED', { execution_kind: 'clear_positions' }), execution(10, 'FAILED', { error: '连接断开' })])
  expect(result.rows.map(recentRowText)).toEqual(['清仓完成', '执行失败 · 最近：连接断开'])
})
test('正常结束但全部未到位，不受 FAILED 与汇总 error 影响', () => {
  const result = rows([execution(23, 'PARTIAL', { error: '2 个品种执行未成功', symbol_results: { m2701: { status: 'PARTIAL' }, rb2701: { status: 'PARTIAL' } } })])
  expect(recentRowText(result.rows[0])).toBe('执行未全部完成 · 2 个品种执行未成功')
})
test('连续未到位独立折叠，只展示最近一次品种', () => {
  const result = rows([execution(23, 'PARTIAL', { symbol_results: { m2701: { status: 'PARTIAL' } } }), execution(22, 'PARTIAL', { symbol_results: { rb2701: { status: 'PARTIAL' } } }), execution(21, 'FAILED')])
  expect(result.rows).toHaveLength(2)
  expect(recentRowText(result.rows[0])).toBe('连续 2 次执行未全部完成 · 1 个品种执行未成功')
  expect(result.rows[1].type).toBe('fail')
})
test('连续失败保留最近一次原因与执行身份', () => {
  const result = rows([execution(23, 'FAILED', { error: '行情连接断开' }), execution(22, 'FAILED')])
  expect(result.rows[0]).toMatchObject({ type: 'fail', count: 2, executionId: 'e23', reason: '行情连接断开' })
})
test('部分成交伴随实际错误仍是失败', () => {
  const result = rows([execution(23, 'FAILED', { error: '拒单', symbol_results: { A: { status: 'SUCCEEDED' }, B: { status: 'FAILED' } } })])
  expect(result.rows[0].type).toBe('fail')
})
test('旧记录一律中性保留且不合并', () => {
  const result = rows([execution(23), execution(22, undefined, { status: 'SUCCEEDED', error: '旧错误' })])
  expect(result.rows.map(recentRowText)).toEqual(['执行状态未知', '调仓完成'])
  expect(result.rows[0]).toMatchObject({ executionId: 'e23' })
})
test('新记录证据缺失显示待确认', () => {
  expect(recentRowText(rows([execution(23, 'UNKNOWN')]).rows[0])).toBe('执行状态未知')
})
test('完成逐条保留，明确 NOOP 可以折叠', () => {
  expect(rows([execution(23, 'SUCCEEDED'), execution(22, 'SUCCEEDED')]).rows).toHaveLength(2)
  expect(rows([execution(23, 'SUCCEEDED', { status: 'NOOP' }), execution(22, 'SUCCEEDED', { status: 'NOOP' })]).rows[0]).toMatchObject({ type: 'noop', count: 2 })
})
test('终止与前置受阻单独分类', () => {
  const result = rows([execution(23, undefined, { task_status: 'TERMINATED' }), execution(22, 'BLOCKED', { error: '当前不在交易时间' })])
  expect(result.rows.map(recentRowText)).toEqual(['执行已终止', '未执行'])
})
test('窗口拉满才显示饱和，限制折叠后的行数', () => {
  const items = [execution(23, 'PARTIAL'), execution(22, 'PARTIAL')]
  expect(rows(items, 6, 2).rows[0]).toMatchObject({ saturated: true })
  expect(rows(items).rows[0]).toMatchObject({ saturated: false })
  expect(rows([execution(23, 'SUCCEEDED'), ...items], 1).truncated).toBe(true)
  expect(rows([]).rows).toEqual([])
})
test('排程跳过与执行相互切断分组', () => {
  const skip: AccountActivity = { kind: 'schedule_skip', id: 1, occurred_at: '2026-09-14T14:00:00', channel: 'ctp', reason_code: 'CALENDAR.CLOSED', calendar_day: '2026-09-14', calendar_id: 'cn', calendar_label: '中国' }
  const result = rows([skip, execution(23, 'PARTIAL'), { ...skip, id: 2 }])
  expect(result.rows.map(r => r.type)).toEqual(['skip', 'partial', 'skip'])
})

test('近期记录展示后端受阻和失败原因', () => {
  const result = rows([
    execution(23, 'BLOCKED', { error: '非交易时段', reason_code: 'COMMON.SESSION.CLOSED' }),
    execution(22, 'FAILED', { error: '交易日历不可用' }),
  ])
  expect(result.rows.map(recentRowText)).toEqual(['休市跳过', '执行失败 · 最近：交易日历不可用'])
})

test('没有 reason_code 的 BLOCKED 不把中文 error 升成非交易时段标题', () => {
  expect(recentRowText(rows([execution(22, 'BLOCKED', { error: '非交易时段' })]).rows[0])).toBe('未执行')
})

test('清仓 NOOP 和 PARTIAL 沿用清仓标题，不与调仓合并', () => {
  const result = rows([execution(23, 'NOOP', { execution_kind: 'clear_positions' }), execution(22, 'NOOP'), execution(21, 'PARTIAL', { execution_kind: 'clear_positions' })])
  expect(result.rows.map(recentRowText)).toEqual(['无需清仓', '无需调仓', '执行未全部完成'])
})
