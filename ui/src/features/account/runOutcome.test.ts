import { expect, test } from 'bun:test'
import { executionRecordView } from './executionOutcome'
import { describeRunOutcome, isTerminalExecutionTaskStatus } from './runOutcome'

test('明确错误才提示失败', () => {
  expect(describeRunOutcome('exec', { status: 'FAILED', error: '连接断开' })).toEqual({ kind: 'failed', error: '执行失败' })
})
test('完成、清仓、终止、受阻分别表达', () => {
  for (const [status, text] of [['SUCCEEDED', '调仓完成'], ['TERMINATED', '执行已终止'], ['BLOCKED', '未执行'], ['UNKNOWN', '执行状态未知']]) {
    expect(describeRunOutcome('exec', { status })).toHaveProperty('toast', text)
  }
  expect(describeRunOutcome('clear', { status: 'SUCCEEDED' })).toEqual({ kind: 'success', toast: '清仓完成' })
})
test('旧结果不根据任务状态翻译成败', () => {
  expect(describeRunOutcome('exec', {})).toEqual({ kind: 'unknown', toast: '执行状态未知' })
})

test('部分休市结束轮询但保留未完成和休市原因', () => {
  expect(isTerminalExecutionTaskStatus('PARTIAL')).toBe(true)
  for (const status of ['QUEUED', 'RUNNING', 'TERMINATING']) expect(isTerminalExecutionTaskStatus(status)).toBe(false)
  const result = describeRunOutcome('exec', {
    status: 'PARTIAL', output_status: 'PARTIAL', error: '部分品种休市，1 个品种未处理',
  })
  const view = executionRecordView({ status: 'PARTIAL', task_status: 'PARTIAL', reason_code: 'COMMON.SESSION.CLOSED', error: '部分品种休市，1 个品种未处理' })
  expect(view.reason).toBe('部分品种休市，1 个品种未处理')
  expect(view.title).toBe('部分品种休市 · 执行未全部完成')
  expect(result.kind).toBe('not_reached')
  expect(result).toHaveProperty('toast', '执行未全部完成')
})
