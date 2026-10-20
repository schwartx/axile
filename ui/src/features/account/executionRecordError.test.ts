import { expect, test } from 'bun:test'
import { executionRecordError } from './executionRecordError'

test('轻量记录与类型化详情使用明确错误字段', () => {
  expect(executionRecordError({ error: '连接断开' })).toBe('连接断开')
  expect(executionRecordError({ error: '收尾失败', result: { error: '此前结果' } })).toBe('收尾失败')
  expect(executionRecordError({ result: { error: '渠道错误' } })).toBe('渠道错误')
  expect(executionRecordError({ error: null, result: { error: '此前结果' } })).toBe('')
  expect(executionRecordError({ error: '', result: { error: '此前结果' } })).toBe('')
  expect(executionRecordError({ result: {} })).toBe('')
})

test('进程中断保留专用提示，不从错误文本推断', () => {
  expect(executionRecordError({ interrupt_reason: 'process_interrupted', error: 'worker 已退出' })).toBe('上次执行中断，未自动续跑')
  expect(executionRecordError({ result: { interrupt_reason: 'process_interrupted' } })).toBe('上次执行中断，未自动续跑')
  expect(executionRecordError({ error: 'process_interrupted' })).toBe('process_interrupted')
})

test('摘要不修改供详情读取的完整错误', () => {
  const error = `连接错误\n${'完整上下文'.repeat(80)}`
  const record = { error }
  expect(executionRecordError(record)).toHaveLength(160)
  expect(record.error).toBe(error)
})

test('展示错误优先，历史异常结果根不参与错误推断', () => {
  expect(executionRecordError({ display_error: '连接失败', error: 'Traceback: source', result: null })).toBe('连接失败')
  expect(executionRecordError({ display_error: '', error: 'Traceback: source', result: [] })).toBe('')
  for (const result of [null, [], '历史结果', 0, false]) expect(executionRecordError({ result })).toBe('')
})
