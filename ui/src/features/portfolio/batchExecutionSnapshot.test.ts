import { expect, test } from 'bun:test'
import { ApiError } from '@/lib/api/client'
import { batchExecutionInfo, type BatchResult } from './batchExecutionSnapshot'

test('batch snapshot copies every accepted id and every complete failure without changing after retries', () => {
  const raw = '错误开始\n' + '长错误'.repeat(200) + '\npassword=synthetic\n尾部根因'
  const failure = new ApiError(409, raw, { code: 'CONFLICT', requestId: 'req-batch', body: { detail: { traceback: raw } } })
  const results: BatchResult[] = [
    { accountId: 7, name: '账户 A', trigger: { execution_id: 'accepted-7', account_id: 7, message: 'queued', accepted: 'created' }, error: null, pending: false },
    { accountId: 8, name: '账户 B', trigger: null, error: failure, pending: false },
    { accountId: 9, name: '账户 C', trigger: null, error: new Error('另一个账户的原文\n完整尾部'), pending: false },
  ]
  const snapshot = batchExecutionInfo(3, results)
  expect(snapshot.text).toContain('组合 ID：3')
  expect(snapshot.text).toContain('账户 A (#7) · 已受理')
  expect(snapshot.text).toContain('accepted-7')
  expect(snapshot.text).toContain('账户 B (#8) · 受理失败')
  expect(snapshot.text).toContain(raw)
  expect(snapshot.text).toContain('HTTP：409')
  expect(snapshot.text).toContain('req-batch')
  expect(snapshot.text).toContain('CONFLICT')
  expect(snapshot.text).toContain('账户 C (#9) · 受理失败')
  expect(snapshot.text).toContain('另一个账户的原文\n完整尾部')
  failure.message = '重试后的错误'
  results[1]!.error = null
  expect(snapshot.text).toContain(raw)
  expect(snapshot.text).not.toContain('重试后的错误')
})

test('pending batch snapshots explicitly distinguish incomplete results and accepted execution requests', () => {
  const info = batchExecutionInfo(3, [{ accountId: 8, name: '账户 B', trigger: null, error: null, pending: true }])
  expect(info.text).toContain('1 个仍在提交，结果尚未齐全')
  expect(info.text).toContain('受理结果待确认')
  expect(info.text).not.toContain('全部请求已返回')
})
