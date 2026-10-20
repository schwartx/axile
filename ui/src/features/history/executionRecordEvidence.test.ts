import { expect, test } from 'bun:test'
import { readExecutionRecordEvidence } from './executionRecordEvidence'
import type { ExecuteRecord, ExecuteRecordList } from '@/types/api'

const detail: ExecuteRecord = {
  account_id: 7, id: 9, execution_id: 'exec-9', is_success: 0, created_at: '2026-10-20T10:00:00',
  input: null, result: { error: '下单失败', technical_detail: '完整错误原文' },
}

test('已知记录直接读取详情，保留无执行标识的历史证据', async () => {
  const saved = { ...detail, execution_id: null }
  const result = await readExecutionRecordEvidence(7, { recordId: 9 }, new AbortController().signal, {
    list: async () => { throw new Error('不应请求摘要') },
    detail: async (accountId, recordId) => { expect([accountId, recordId]).toEqual([7, 9]); return saved },
  })
  expect(result).toBe(saved)
  expect(result?.result).toEqual({ error: '下单失败', technical_detail: '完整错误原文' })
})

test('执行标识先定位摘要，再按记录读取完整详情', async () => {
  const calls: string[] = []
  const result = await readExecutionRecordEvidence(7, { executionId: 'exec-9' }, new AbortController().signal, {
    list: async (_accountId, query) => {
      expect(query).toEqual({ execution_id: 'exec-9', limit: 1 })
      calls.push('list')
      return { data: [{ account_id: 7, id: 9, execution_id: 'exec-9', is_success: 0, created_at: detail.created_at }], count: 1 }
    },
    detail: async () => { calls.push('detail'); return detail },
  })
  expect(calls).toEqual(['list', 'detail'])
  expect(result).toBe(detail)
})

test('尚未生成记录保持缺失，摘要与详情错配显示读取错误', async () => {
  const list = async (): Promise<ExecuteRecordList> => ({ data: [], count: 0 })
  expect(await readExecutionRecordEvidence(7, { executionId: 'pending' }, new AbortController().signal, {
    list, detail: async () => { throw new Error('不应请求详情') },
  })).toBeNull()
  await expect(readExecutionRecordEvidence(7, { recordId: 9, executionId: 'other' }, new AbortController().signal, {
    list, detail: async () => detail,
  })).rejects.toThrow('与选中记录不一致')
  await expect(readExecutionRecordEvidence(8, { recordId: 9 }, new AbortController().signal, {
    list, detail: async () => detail,
  })).rejects.toThrow('与选中记录不一致')
})

test('定位后取消不再发起详情，详情读取失败保留原始错误', async () => {
  const controller = new AbortController()
  await expect(readExecutionRecordEvidence(7, { executionId: 'exec-9' }, controller.signal, {
    list: async () => { controller.abort(); return { data: [detail], count: 1 } },
    detail: async () => { throw new Error('不应请求详情') },
  })).rejects.toThrow()
  const failure = new Error('详情读取失败')
  await expect(readExecutionRecordEvidence(7, { recordId: 9 }, new AbortController().signal, {
    list: async () => ({ data: [], count: 0 }), detail: async () => { throw failure },
  })).rejects.toBe(failure)
})
